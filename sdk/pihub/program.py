"""Program: an application hosted by a Device.

A Program represents an application hosted by a Device (Bird Box, Weather
Station, ...). Its public API covers outputs, commands, notifications and
lifecycle events:

    birdbox = Program(id="birdbox", name="Bird Box")

    temperature = birdbox.output(id="temperature", type="Gauge")
    temperature.set(22.5)

    snapshot = birdbox.command(id="snapshot", type="Button")

    @snapshot.handler
    def take_snapshot(command):
        birdbox.info("Snapshot captured")

    @birdbox.on_first_user_connected
    def start_camera(payload):
        ...

    device.add_program(birdbox)
    device.start()

A Program is constructed and configured (outputs, commands, handlers)
before it is necessarily attached to a `Device` - the example above builds
`temperature` and wires `take_snapshot` before `device.add_program(birdbox)`
runs. Widget registrations, sending values/notifications, and subscribing
handlers on the shared Event Bus
(`lifecycle.<event>`, `command.<widgetId>`) all need a device id and the
device's `EventBus`, which only exist once attached. So:

- `output()` / `command()` just record widgets locally; the manifest is
  built lazily by `to_registration()`, which `Device.add_program()` /
  `Device.start()` already know how to call.
- `.handler` / `@on_*` decorators queue their subscription if not yet
  attached, or subscribe immediately on `device.events` if already
  attached.
- `Device.add_program()` calls an optional `_bind_device(device)` hook
  (see device.py) once the program is stored, which supplies the device id
  and flushes any queued subscriptions.
- `output.set()` / `.set_values()` and `info()` / `warning()` / `error()`
  raise `RuntimeError` if called before attachment, since they need a
  device id to build a `Binding` and a transport to send through.

`Output` and `Command` themselves live in `pihub.widgets` (per the Device
SDK spec's "Package Structure": `widgets/output.py`, `widgets/command.py`)
- `Program` constructs them and calls their `bind(self)` so they can call
back into `_publish_runtime()` / `_register_command_handler()` below.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Tuple

from pihub.properties.base import Property
from pihub.properties.boolean import BooleanProperty
from pihub.properties.number import NumberProperty
from pihub.properties.select import SelectProperty
from pihub.properties.string import StringProperty
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import (
    LIFECYCLE_FIRST_USER_CONNECTED,
    LIFECYCLE_LAST_USER_DISCONNECTED,
    LIFECYCLE_VIEWER_COUNT_CHANGED,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    NotificationMessage,
    ProgramRegistration,
    RuntimeMessage,
)
from pihub.widgets import Command, Output, Setting, Widget
from pihub.widgets.camera import CAMERA, Camera
from pihub.widgets.files import FILES, Files
from pihub.widgets.setting import SELECT, SLIDER, TEXT, TOGGLE
from pihub.widgets.command import CommandHandler

if TYPE_CHECKING:
    from pihub.device import Device

LifecycleHandler = Callable[[Dict[str, Any]], None]

logger = logging.getLogger("pihub.program")

SAMPLE_SECONDS = 2.0
REFRESH_MODES = ("static", "live", "always")


def _with_group(id: str, properties: Optional[List[Property]], group: Optional[str]) -> List[Property]:
    """The widget's properties, plus a `group` string property when a group was given (it travels as an ordinary default property)."""
    result = list(properties) if properties else []
    if group is None:
        return result
    if not isinstance(group, str) or not group.strip():
        raise ValueError(f"widget {id!r}: group must be a non-empty string, not {group!r}")
    if any(getattr(item, "name", None) == "group" for item in result):
        raise ValueError(f"widget {id!r}: give the group once, either as group= or as a property")
    result.append(StringProperty(name="group", label="Group", default=group.strip()))
    return result


class Program:
    """Represents an application hosted by a device."""

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name
        self._widgets: Dict[str, Widget] = {}
        self._device: Optional["Device"] = None
        self._pending_command_handlers: List[Tuple[str, CommandHandler]] = []
        self._pending_lifecycle_handlers: List[Tuple[str, LifecycleHandler]] = []
        # How often a "live" or "always" pull-style output is read when it does not say (`output(..., every=...)`).
        self.sample_seconds: float = SAMPLE_SECONDS
        # Where `setting()` values are saved (a `pihub.settings_store.SettingsStore`); the default is `settings.json` beside config.json.
        self.settings_store: Optional[Any] = None
        self._excluded: Set[str] = set()  # ids removed on purpose: things the SDK adds later (the camera's settings) skip them
        self._sampling_lock = threading.Lock()
        self._stop_sampling: Optional[threading.Event] = None
        self._stop_always: Optional[threading.Event] = None
        self._sampling_wired = False
        self._tasks: List[Tuple[float, Callable[[], Any], str]] = []  # `every(...)`: periodic work that runs while the device does
        self._stop_tasks: Optional[threading.Event] = None

    # -- widgets ----------------------------------------------------------

    def output(
        self,
        id: str,
        type: str,
        properties: Optional[List[Property]] = None,
        *,
        read: Optional[Callable[[], Any]] = None,
        refresh: Optional[str] = None,
        every: Optional[float] = None,
        background: bool = False,
        group: Optional[str] = None,
    ) -> Output:
        """Register an output widget that publishes runtime values.

        `group` (optional, on `output`, `command` and `setting` alike) is a **hint** that this component belongs with others of the same name, for example
        `group="Camera"`. It is sent as an ordinary `group` property; the dashboard may lay the section out by it or ignore it, and a device without one is
        unchanged.

        Push it yourself with `.set(value)`, or hand the section a **reader** and let it do the publishing: `read` is called (it returns the value, or
        None for "nothing to show") according to `refresh`:

        * `"static"` (the default when `read` is given): once, after the device has started. A slow reader can pass `background=True` to run on a thread;
        * `"live"`: every `every` seconds (default `sample_seconds`, 2 s) **only while someone is viewing the device**, so an idle device is not
          reading, and the platform is not fed, values nobody sees. A reader with a `reset()` method has it called each time sampling starts;
        * `"always"`: every `every` seconds for as long as the device is running.
        """
        if read is None and (refresh is not None or every is not None or background):
            raise ValueError(f"output {id!r}: refresh, every and background only apply to an output that has a `read` function")
        if read is not None:
            refresh = refresh or "static"
            if refresh not in REFRESH_MODES:
                raise ValueError(f"output {id!r}: refresh must be one of {', '.join(REFRESH_MODES)}, not {refresh!r}")
            if every is not None and every <= 0:
                raise ValueError(f"output {id!r}: every must be positive")
        self._claim_widget_id(id)
        # "Camera" and "Files" are outputs that also take actions (components): their own classes, answering their own commands.
        widget_class = Camera if type == CAMERA else Files if type == FILES else Output
        widget = widget_class(id=id, type=type, properties=_with_group(id, properties, group))
        widget.bind(self)
        if isinstance(widget, (Camera, Files)):
            self._register_command_handler(id, widget.handle)
        widget.read, widget.refresh, widget.every, widget.background = read, refresh, every, background
        self._widgets[id] = widget
        if refresh == "live":
            self._wire_sampling()
        return widget

    def setting(
        self,
        id: str,
        type: str,
        *,
        default: Any = None,
        title: Optional[str] = None,
        units: Optional[str] = None,
        options: Optional[List[Any]] = None,
        minimum: Optional[float] = None,
        maximum: Optional[float] = None,
        step: Optional[float] = None,
        max_length: Optional[int] = None,
        apply: Optional[Callable[[Any], None]] = None,
        store: Optional[Any] = None,
        persist: bool = True,
        group: Optional[str] = None,
    ) -> Setting:
        """Register a **settable** component: it shows its current value and lets the dashboard change it.

        `type` is `"Toggle"` (true or false), `"Select"` (one of `options`), `"Slider"` (a number from `minimum` to `maximum`, in steps of `step`)
        or `"Text"` (a string, no longer than `max_length` if you gave one). The device checks every change itself, saves an accepted one (so it
        survives a restart; `persist=False` keeps it in memory only, `store=` saves it somewhere else), publishes it, and then calls `apply(value)`
        if you gave one. The value in force is `.value`; `.set(...)` changes it from code. A saved value wins over `default`, which is what a device
        with nothing saved starts with.
        """
        self._claim_widget_id(id)
        label = title or id.replace("-", " ").replace("_", " ").capitalize()
        properties: List[Property] = [StringProperty("title", default=label)]
        if units is not None:
            properties.append(StringProperty("units", default=units))
        if type == TOGGLE:
            default = False if default is None else default
            properties.append(BooleanProperty("value", default=bool(default)))
        elif type == SELECT:
            properties.append(SelectProperty("value", default=default, options=list(options or [])))
        elif type == SLIDER:
            properties.append(NumberProperty("value", default=default, minimum=minimum, maximum=maximum, step=step))
        elif type == TEXT:
            default = "" if default is None else default
            properties.append(StringProperty("value", default=default, max_length=max_length))
        else:
            raise ValueError(f"a setting is a Toggle, a Select, a Slider or a Text, not {type!r}")
        properties = _with_group(id, properties, group)
        if store is None and persist:
            if self.settings_store is None:
                from pihub.settings_store import SettingsStore

                self.settings_store = SettingsStore()
            store = self.settings_store
        widget = Setting(
            id=id, type=type, properties=properties, default=default, options=list(options) if options else None,
            minimum=minimum, maximum=maximum, step=step, max_length=max_length, apply=apply,
            store=store if persist or store else None, store_key=f"{self.id}.{id}",
        )
        widget.bind(self)
        self._widgets[id] = widget
        self._register_command_handler(id, lambda message: self._widgets.get(id) is widget and widget.on_command(message))
        return widget

    def widget(self, id: str) -> Optional[Widget]:
        """The widget registered under `id`, or None."""
        return self._widgets.get(id)

    def remove(self, id: str) -> None:
        """Take a component out of this section. Only before the device starts (the manifest is sent then and is fixed).

        Removing an id that is not there yet is fine and is remembered: something the SDK adds later (the camera's settings) leaves it out. Adding
        that id yourself afterwards puts it back.
        """
        if self._started():
            raise RuntimeError(f"cannot remove {id!r} from section {self.id!r}: the device has already started")
        self._widgets.pop(id, None)
        self._excluded.add(id)

    def every(self, seconds: float, func: Callable[[], Any], *, name: Optional[str] = None) -> None:
        """Run `func` every `seconds` for as long as the device is running, whether or not anyone is viewing it (Device Information uses this to
        watch temperature, CPU and disk). The first run is one interval after the device has started. A run that raises is skipped, never fatal.
        Before `start()` only."""
        if seconds <= 0:
            raise ValueError("every: seconds must be positive")
        if self._started():
            raise RuntimeError(f"program {self.id!r}: every() can only be used before the device starts")
        self._tasks.append((seconds, func, name or getattr(func, "__name__", "task")))

    def _run_task(self, seconds: float, func: Callable[[], Any], stop: threading.Event) -> None:
        while not stop.wait(seconds):
            try:
                func()
            except Exception:
                logger.debug("a periodic task of %s raised", self.id, exc_info=True)

    def is_excluded(self, id: str) -> bool:
        """Whether `id` was removed on purpose, so the SDK should not add it."""
        return id in self._excluded

    def _started(self) -> bool:
        return self._device is not None and bool(getattr(self._device, "_started", False))

    def video_sources(self) -> Dict[str, Any]:
        """`{streamId: source}` for every Video or Camera output that has a source attached."""
        return {
            widget.stream_id: widget.source
            for widget in self._widgets.values()
            if isinstance(widget, Output) and widget.source is not None
        }

    def resolve_file(self, widget_id: str, folder: str, name: str) -> Optional[Any]:
        """The real path `widget_id`'s `folder`/`name` names, or None - only a `Files` widget answers this
        (`PeerManager` calls it, through `Device`, before opening a download)."""
        widget = self._widgets.get(widget_id)
        return widget.resolve(folder, name) if isinstance(widget, Files) else None

    def command(self, id: str, type: str, properties: Optional[List[Property]] = None, *, group: Optional[str] = None) -> Command:
        """Register a command widget that receives actions. `group` is a hint for the dashboard: see `output`."""
        self._claim_widget_id(id)
        widget = Command(id=id, type=type, properties=_with_group(id, properties, group))
        widget.bind(self)
        self._widgets[id] = widget
        return widget

    def _claim_widget_id(self, id: str) -> None:
        if id in self._widgets:
            raise ValueError(f"widget {id!r} is already registered on program {self.id!r}")
        self._excluded.discard(id)  # adding it yourself after removing it puts it back

    def to_registration(self) -> ProgramRegistration:
        """Build this program's manifest entry, per "Device Manifest Generation"."""
        return ProgramRegistration(
            program_id=self.id,
            name=self.name,
            widgets=[widget.to_registration() for widget in self._widgets.values()],
        )

    # -- notifications ------------------------------------------------------

    def info(self, message: str) -> None:
        self._notify(SEVERITY_INFO, message)

    def warning(self, message: str) -> None:
        self._notify(SEVERITY_WARNING, message)

    def error(self, message: str) -> None:
        self._notify(SEVERITY_ERROR, message)

    def _notify(self, severity: str, message: str) -> None:
        device = self._require_device()
        notification = NotificationMessage(
            device_id=device.id, program_id=self.id, severity=severity, message=message
        )
        device.send(notification)
        device.events.publish(f"notification.{severity}", notification)

    # -- lifecycle decorators ------------------------------------------------

    def on_first_user_connected(self, func: LifecycleHandler) -> LifecycleHandler:
        self._register_lifecycle_handler(LIFECYCLE_FIRST_USER_CONNECTED, func)
        return func

    def on_last_user_disconnected(self, func: LifecycleHandler) -> LifecycleHandler:
        self._register_lifecycle_handler(LIFECYCLE_LAST_USER_DISCONNECTED, func)
        return func

    def on_viewer_count_changed(self, func: LifecycleHandler) -> LifecycleHandler:
        self._register_lifecycle_handler(LIFECYCLE_VIEWER_COUNT_CHANGED, func)
        return func

    # -- pull-style outputs: static, live and always ------------------------------------------------------------------

    def _pull(self, refresh: str) -> List[Output]:
        return [w for w in self._widgets.values() if isinstance(w, Output) and w.read is not None and w.refresh == refresh]

    def _wire_sampling(self) -> None:
        """Subscribe, once, to the viewer events that start and stop the live readings."""
        if self._sampling_wired:
            return
        self._sampling_wired = True
        self.on_first_user_connected(lambda payload: self._start_sampling())
        self.on_last_user_disconnected(lambda payload: self._stop_sampling_now())
        self.on_viewer_count_changed(self._count_changed)

    def _device_started(self) -> None:
        """Called by `Device` once it has started. A section cannot publish before this: it is not attached to a started device until now.

        Publishes each setting's current value, reads the `static` outputs once, takes one first reading of the `live` ones, and starts the
        `always` ones. Nothing here raises. A section with none of these does nothing.
        """
        for widget in list(self._widgets.values()):
            if isinstance(widget, Setting):
                widget.publish()
            elif isinstance(widget, Camera):
                widget.device_started()
            elif isinstance(widget, Files):
                widget.publish()
        for output in self._pull("static"):
            if output.background:
                threading.Thread(target=self._read_into, args=(output,), daemon=True, name=f"{self.id}-{output.id}").start()
            else:
                self._read_into(output)
        self._publish_group("live", only_due=False)
        if self._pull("always"):
            stop = threading.Event()
            self._stop_always = stop
            threading.Thread(target=self._loop, args=("always", stop), daemon=True, name=f"{self.id}-always").start()
        if self._tasks:
            stop_tasks = threading.Event()
            self._stop_tasks = stop_tasks
            for seconds, func, task_name in self._tasks:
                threading.Thread(target=self._run_task, args=(seconds, func, stop_tasks), daemon=True, name=f"{self.id}-{task_name}").start()

    def _device_stopped(self) -> None:
        for widget in list(self._widgets.values()):
            if isinstance(widget, Camera):
                widget.device_stopped()
        self._stop_sampling_now()
        stop, self._stop_always = self._stop_always, None
        if stop is not None:
            stop.set()
        stop_tasks, self._stop_tasks = self._stop_tasks, None
        if stop_tasks is not None:
            stop_tasks.set()

    @property
    def sampling(self) -> bool:
        """Whether the live readings are being taken now (a viewer is present)."""
        return self._stop_sampling is not None

    def _count_changed(self, payload: Dict[str, Any]) -> None:
        try:
            count = int(payload.get("viewerCount", 0))
        except (TypeError, ValueError):
            return
        # A safety net for a viewer that was already there when the device reconnected, or a "last viewer" that never arrived.
        if count > 0:
            self._start_sampling()
        else:
            self._stop_sampling_now()

    def _start_sampling(self) -> None:
        with self._sampling_lock:
            if self._stop_sampling is not None:
                return
            stop = threading.Event()
            self._stop_sampling = stop
        for output in self._pull("live"):
            reset = getattr(output.read, "reset", None)  # e.g. a CPU meter: the first number must not average over an idle gap
            if callable(reset):
                try:
                    reset()
                except Exception:
                    logger.debug("could not reset the reader for %s", output.id, exc_info=True)
        threading.Thread(target=self._loop, args=("live", stop), daemon=True, name=f"{self.id}-sampler").start()

    def _stop_sampling_now(self) -> None:
        with self._sampling_lock:
            stop, self._stop_sampling = self._stop_sampling, None
        if stop is not None:
            stop.set()

    def _loop(self, refresh: str, stop: threading.Event) -> None:
        self._publish_group(refresh, only_due=False)
        while not stop.wait(self._tick(refresh)):
            self._publish_group(refresh, only_due=True)

    def _tick(self, refresh: str) -> float:
        intervals = [o.every or self.sample_seconds for o in self._pull(refresh)]
        return max(0.005, min(intervals)) if intervals else self.sample_seconds

    def _publish_live(self) -> None:
        """One reading of every live output now."""
        self._publish_group("live", only_due=False)

    def _publish_group(self, refresh: str, only_due: bool) -> None:
        now = time.monotonic()
        for output in self._pull(refresh):
            if only_due and now < output.next_due:
                continue
            output.next_due = now + (output.every or self.sample_seconds)
            self._read_into(output)

    def _read_into(self, output: Output) -> None:
        """Read one output and publish what it returns. A reader that raises, returns None, or cannot be published just skips this reading."""
        try:
            value = output.read() if output.read is not None else None
        except Exception:
            logger.debug("could not read %s", output.id, exc_info=True)
            return
        if value is None:
            return
        try:
            output.set(value)
        except Exception:  # not connected right now: the next reading (or the reconnect's re-send) covers it
            logger.debug("could not publish %s", output.id, exc_info=True)

    # -- runtime plumbing -----------------------------------------------------

    def _publish_runtime(
        self, widget_id: str, *, value: Any = None, values: Optional[Dict[str, Any]] = None
    ) -> None:
        device = self._require_device()
        binding = Binding(device_id=device.id, program_id=self.id, widget_id=widget_id)
        device.send(RuntimeMessage(binding=binding, value=value, values=values))

    def _register_command_handler(self, widget_id: str, handler: CommandHandler) -> None:
        if self._device is not None:
            self._device.events.subscribe(f"command.{widget_id}", handler)
        else:
            self._pending_command_handlers.append((widget_id, handler))

    def _register_lifecycle_handler(self, event: str, handler: LifecycleHandler) -> None:
        if self._device is not None:
            self._device.events.subscribe(f"lifecycle.{event}", handler)
        else:
            self._pending_lifecycle_handlers.append((event, handler))

    def _require_device(self) -> "Device":
        if self._device is None:
            raise RuntimeError(f"program {self.id!r} is not attached to a device")
        return self._device

    def _bind_device(self, device: "Device") -> None:
        """Called by `Device.add_program()` once this program is registered."""
        if self._device is not None:
            raise RuntimeError(f"program {self.id!r} is already attached to a device")

        self._device = device
        for widget_id, handler in self._pending_command_handlers:
            device.events.subscribe(f"command.{widget_id}", handler)
        for event, handler in self._pending_lifecycle_handlers:
            device.events.subscribe(f"lifecycle.{event}", handler)
        self._pending_command_handlers.clear()
        self._pending_lifecycle_handlers.clear()


# A "Section" is what the dashboard shows as one collapsible panel of components. It is the same class as `Program`, which stays as the older name
# for a section that is an application (a bird box, a weather station); `Section` is the name for the general idea, and what to use for Device
# Information. The wire and the platform still call it a program (`programId`): that is part of the frozen protocol.
Section = Program

