"""Device: the physical or virtual endpoint that hosts programs and talks to the platform.

The Device represents a physical or virtual endpoint (Raspberry Pi, Linux
server, container, VM). Its public API is exactly what the spec's "Device
API" and "V1 Required SDK Features > Device" sections document:

    device = Device(id="birdbox01")   # or Device(), once `pihub setup` has
                                      # registered this machine (id from config)
    device.add_program(birdbox)
    device.start()
    device.stop()

Per "Device Responsibilities" (Registration, Transport Management, Program
Management, Lifecycle Processing), Device is the layer that:

- Builds (via `pihub.registration.build_device_manifest` - the single
  source of truth for the manifest; `Device` never constructs the
  registration message itself) and sends it on start() ("Device Manifest
  Generation": developers should never manually build protocol messages).
- Owns the transport connection and feeds inbound wire messages into the
  internal Event Bus, using the naming convention from "Event Bus
  Integration" (`lifecycle.<event>`, `command.<widgetId>`).

`add_program()` accepts any object that structurally satisfies
`RegistrableProgram` below (an id plus a `to_registration()` that returns
the existing `ProgramRegistration` protocol dataclass) rather than
importing the concrete `Program` class, keeping Device decoupled from it.
If the program also defines an optional `_bind_device(device)` hook (as
`Program` does), `add_program()` calls it so the program can learn its
device id and subscribe its handlers on `device.events` - the one shared,
per-device Event Bus that `handle_incoming()` publishes onto. Likewise the
transport is a minimal duck-typed `Transport` protocol, since no concrete
WebRTC/WebSocket transport is built in and the protocol is transport-agnostic.
"""

from __future__ import annotations

import logging
import os
import platform
import socket
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple, Union, runtime_checkable

from pihub.device_config import DeviceConfig, default_config_path, load_device_config, save_device_config
from pihub.device_lock import DeviceLock, acquire as acquire_device_lock
from pihub.device_identity import collect_fingerprint
from pihub.device_runtime_service import DeviceRuntimeService
from pihub.events.bus import EventBus
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import (
    CommandMessage,
    LifecycleMessage,
    MESSAGE_TYPE_COMMAND,
    MESSAGE_TYPE_LIFECYCLE,
    MESSAGE_TYPE_SIGNAL,
    NotificationMessage,
    ProgramRegistration,
    RegistrationMessage,
    RuntimeMessage,
    SignalMessage,
    parse_message,
)
from pihub.protocol.envelope import Envelope
from pihub.registration.manifest_builder import build_device_manifest
from pihub.reconnect import REFUSED_ADVICE, REFUSED_STATUSES, STABLE_AFTER_SECONDS, Backoff
from pihub.link_code_client import LinkExpiredError, poll_until_approved, request_link as request_link_code
from pihub.registration_client import (
    DEFAULT_SERVER,
    SERVER_ENV,
    HttpPost,
    RegistrationError,
    default_http_post,
    exchange_registration_token,
)
from pihub.security import InsecureTransportError, require_secure_url

if TYPE_CHECKING:  # the WebRTC package is imported lazily, only when it is used
    from pihub.device_information import DeviceInformation
    from pihub.webrtc import DeviceLimits

OutgoingMessage = Union[RegistrationMessage, RuntimeMessage, NotificationMessage]

logger = logging.getLogger(__name__)


def _stdin_is_interactive() -> bool:
    """Whether a person is at a terminal: standard input is one. What `Device(register_if_needed=None)` (the default) decides on.

    A service, a cron job or a pipe has no terminal, so nothing there can be asked to approve a link code and nothing there ever
    waits for one. Its own function so a test can say which it is.
    """
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):  # a closed or detached standard input
        return False


@runtime_checkable
class RegistrableProgram(Protocol):
    """The minimal shape Device needs from a program-like object.

    Not part of the SDK's public API surface - it exists only to type-hint
    `add_program()` until a concrete `Program` class is implemented.
    """

    id: str

    def to_registration(self) -> ProgramRegistration: ...


@runtime_checkable
class Transport(Protocol):
    """The minimal shape Device needs from a transport-like object.

    Not part of the SDK's public API surface. A concrete transport
    (WebRTC, WebSocket, ...) is out of scope for the Device SDK layer: the
    protocol is transport-independent.
    """

    def send(self, data: str) -> None: ...


_ARM_32BIT = ("armv6l", "armv7l", "armv8l")  # what `platform.machine()` says on a 32-bit Raspberry Pi (armv8l: a 32-bit userland on a 64-bit kernel)


def _webrtc_install_hint() -> str:
    """What to do when the WebRTC library is missing. On a 32-bit Pi pip cannot install it (PyAV has no wheels there), so the install skips it and apt is the route."""
    if platform.machine() in _ARM_32BIT:
        return (
            "On a 32-bit Raspberry Pi pip cannot install it: run `sudo apt install python3-aiortc python3-av`, "
            "and make the virtual environment with `--system-site-packages`."
        )
    return "Try `pip install --upgrade aiortc`; if it is already installed, one of its native parts may be broken (run `python -c 'import aiortc'` to see the error)."


class Device:
    """Represents a physical or virtual device hosting one or more programs."""

    def __init__(
        self,
        id: Optional[str] = None,
        transport: Optional[Transport] = None,
        *,
        config_path: Optional[Path] = None,
        reconnect: Optional[bool] = None,
        webrtc: Optional[bool] = None,
        limits: Optional["DeviceLimits"] = None,
        device_information: Union[None, bool, "DeviceInformation"] = None,
        register_if_needed: Optional[bool] = None,
        server: Optional[str] = None,
    ) -> None:
        """`register_if_needed`: if `start()` finds no saved
        config at all, register interactively first - print a link code, block until a browser
        approves it (the same mechanism `pihub setup` uses), then proceed as normal. **The default, `None`,
        is automatic: it does so exactly when a person is at a terminal** (standard input is one), which is
        the whole point - registering the first time and connecting immediately every time after, with
        nothing to write. With no terminal (a systemd service, cron, a pipe) it never does, so a program
        started at boot cannot sit waiting for a human: it behaves as before, and `Device()` with no id and
        no saved config is an error saying how to register. `True` forces the prompt even without a
        terminal; `False` never prompts. Give an `id` to choose the
        device's own id; leave it out and the platform assigns one (as `pihub setup` does), which the
        device then keeps in its saved config - `device.id` is `None` until `start()` has registered.
        A transport given means `start()` never registers. Left out with no saved config and no
        registering (no terminal, or `False`) there is nothing to go on: an error.
        `server` is where to register if
        needed - default `$PIHUB_SERVER` or the production platform, same resolution `pihub` the CLI
        uses; unused once a config exists, since the server then comes from it, same as always.

        `reconnect` controls what happens when the connection is lost:
        keep retrying in the background (True), or let the receive loop end
        as it always did (False). The default, `None`, means True for a device
        that built its own connection from a saved registration - the normal
        case for a real device - and False when the caller supplied a
        transport, since that is a simulation or test the caller is driving.

        `webrtc` controls whether browsers may connect to this device directly
        (WebRTC; the library comes with `pip install thepihub`, except on a
        32-bit Raspberry Pi, where it comes from apt). The default, `None`, means yes
        for a device that built its own connection and has the library
        installed, and no otherwise; `PIHUB_WEBRTC=0` turns it off. A device
        without the library still works: the platform gives its viewers a
        relayed session, and the device logs one warning saying why direct
        connections are off (`webrtc=False` or `PIHUB_WEBRTC=0` keeps it quiet).
        `limits` overrides how many viewers this device will carry (`DeviceLimits`).

        `device_information` controls the built-in "Device Information" program (model, OS, CPU, temperature,
        memory, power ... see `pihub.device_information`). The default, `None`, means yes for a device that
        built its own connection and no otherwise (a simulation or test that passes its own transport is left as
        it was); `True` / `False` force it; a `DeviceInformation(...)` turns it on with options.
        """
        self._config_path = config_path if config_path is not None else default_config_path()
        self._device_lock: Optional[DeviceLock] = None
        will_register = _stdin_is_interactive() if register_if_needed is None else register_if_needed
        if id is None:
            # A device onboarded with `pihub setup` learns its id from its
            # config, not from source code - the platform issued it.
            config = load_device_config(self._config_path)
            if config is not None:
                id = config.device_id
            elif not will_register or transport is not None:  # a transport given means start() never registers
                raise ValueError(
                    "no device id: pass Device(id=...), or register this machine first "
                    "with `pihub setup` (no config found at "
                    f"{self._config_path}), or run this program at a terminal, where it links itself "
                    "the first time (register_if_needed=True does that even without one)"
                )
            # else: no id yet - start() registers, and the platform assigns it.
        self.id: Optional[str] = id
        self.events = EventBus()
        self._transport = transport
        self._programs: Dict[str, RegistrableProgram] = {}
        self._started = False
        self._ever_started = False
        self._runtime_service: Optional[DeviceRuntimeService] = None
        self._reconnect_setting = reconnect
        self._webrtc_setting = webrtc
        self._limits = limits
        self._information_setting = device_information
        self._register_if_needed = will_register
        self._server_setting = server
        self._information: Optional[Any] = None  # the section this Device built, if any
        self._peers: Optional[Any] = None  # a `pihub.webrtc.PeerManager`, when enabled
        self._reconnect = False
        self._backoff = Backoff()
        self._clock = time.monotonic
        self._connected_since: Optional[float] = None
        # The latest runtime message per widget. Runtime values are current
        # state, and the platform holds them only in memory, so after a
        # reconnect (often to a freshly restarted server) they are re-sent -
        # otherwise the dashboard stays empty until the next change.
        self._latest_runtime: Dict[Tuple[str, str], RuntimeMessage] = {}

    def add_program(self, program: RegistrableProgram) -> None:
        """Register a program to be hosted by this device.

        Per "Device Manifest Generation", the program's widgets are folded
        into the registration manifest automatically on `start()`.
        """
        if self._started:
            raise RuntimeError(
                f"cannot add program {program.id!r}: device {self.id!r} already started"
            )
        if program.id in self._programs:
            raise ValueError(f"program {program.id!r} is already registered")

        self._programs[program.id] = program

        bind_device = getattr(program, "_bind_device", None)
        if callable(bind_device):
            bind_device(self)

    def start(self) -> None:
        """Connect, register, and start receiving - the device is fully
        running when this returns.

        `device.start()` alone must be sufficient to run a
        device:

            connect transport -> send registration -> start background receive loop

        Registering the device, its programs and their widgets happens
        automatically - developers never build protocol messages by hand.
        The device's fingerprint is collected and attached the same way.

        The background receive loop (a `DeviceRuntimeService`) only
        starts if the transport can actually receive - see
        `pihub.device_runtime_service.ReceivingTransport`. `ConsoleTransport`
        has no inbound message source, so nothing starts for it; this
        method still works exactly as before for such a transport.

        Per the Authentication and Device Registration Implementation
        Prompt: if no `transport` was given at construction time *and*
        this device has a persisted secret (from a prior `register()`
        call), a `WebSocketTransport` authenticated with that secret is
        built automatically - see `_build_authenticated_transport()`.
        A device constructed with an explicit transport is never
        second-guessed here; this only fills in the gap for the "nothing
        was passed" case.
        """
        if self._started:
            raise RuntimeError(f"device {self.id!r} already started")

        self._started = True
        self._ever_started = True

        if self._register_if_needed and self._transport is None and load_device_config(self._config_path) is None:
            self._run_register_if_needed()

        built_here = False
        if self._transport is None:
            self._transport = self._build_authenticated_transport()
            built_here = self._transport is not None

        if built_here:
            # One program at a time per saved identity (device_lock.py). Only for a connection built from the saved
            # identity: a device given its own transport (the tests, a console device) shares nothing with a neighbour.
            try:
                self._device_lock = acquire_device_lock(self._config_path, f"device {self.id!r}")
            except BaseException:
                self._started = False
                raise

        try:
            self._install_device_information(built_here)  # before the manifest is built
            wanted = self._reconnect_setting if self._reconnect_setting is not None else built_here
            self._reconnect = bool(wanted) and self._can_reconnect()
            self._start_webrtc(built_here)

            if self._reconnect:
                self._first_connection()
            else:
                self._connect_transport()
                manifest = build_device_manifest(self.id, self._programs.values(), collect_fingerprint())
                self.send(manifest)
                if self._peers is not None:
                    self._peers.announce()

            self._start_runtime_service()
            self._publish_stream_ids()
            self._notify_programs_started()
        except BaseException:
            self._release_device_lock()  # a start that failed must not keep the identity locked
            raise

    def _release_device_lock(self) -> None:
        lock, self._device_lock = self._device_lock, None
        if lock is not None:
            lock.release()

    def section(self, id: str, name: str) -> Any:
        """A section (a named panel of components on the dashboard) of this device: the one with this id if it exists, else a new one.

        `device.section("garden", "Garden")` is `Section("garden", "Garden")` followed by `add_program`. Before `start()` only.
        """
        existing = self._programs.get(id)
        if existing is not None:
            return existing
        from pihub.program import Program

        section = Program(id, name)
        self.add_program(section)
        return section

    @property
    def information(self) -> Any:
        """The **Device Information** section, with the standard fields already in it: add your own with `info.output(...)`, or take one away with
        `info.remove("wifi-signal")`. Only before `start()`, when the manifest is built. Asking for it turns Device Information on, even for a device
        given its own transport; a program of your own with the id `device-information` takes its place."""
        from pihub.device_information import DeviceInformation, build_information_section, DEVICE_INFORMATION_PROGRAM_ID

        existing = self._programs.get(DEVICE_INFORMATION_PROGRAM_ID)
        if existing is not None:
            return existing
        if self._started:
            raise RuntimeError(f"device {self.id!r} has already started without Device Information")
        return self._create_information()

    def _create_information(self) -> Any:
        """Build the Device Information section and put it FIRST, whatever order programs were added in. (`start()` calls this after it has marked
        itself started, which is why the public accessor's "too late" check is not in here.)"""
        from pihub.device_information import build_information_section

        section = build_information_section(lambda: self.id, self._information_options())  # the id may not exist yet (first start, registering)
        section._bind_device(self)
        self._programs = {section.id: section, **self._programs}
        self._information = section
        return section

    def _information_options(self) -> Any:
        from pihub.device_information import DeviceInformation

        setting = self._information_setting
        return setting if isinstance(setting, DeviceInformation) else DeviceInformation()

    def _install_device_information(self, built_here: bool) -> None:
        """Add the built-in Device Information section, FIRST, unless it is off or the device already has a program by that id; and add what a real
        camera contributes to it."""
        # Imported here, not at the top: this module stays decoupled from the concrete `Program` class (see its header).
        from pihub.device_information import DEVICE_INFORMATION_PROGRAM_ID, add_camera_information, add_camera_settings

        setting = self._information_setting
        enabled = built_here if setting is None else bool(setting)
        if DEVICE_INFORMATION_PROGRAM_ID not in self._programs and not enabled:
            return
        section = self._programs.get(DEVICE_INFORMATION_PROGRAM_ID) or self._create_information()
        if section is not self._information:
            return  # the device's own program with that id: it manages itself
        sources = [
            source for program in self._programs.values() for source in getattr(program, "video_sources", lambda: {})().values()
        ]
        camera = next((source for source in sources if getattr(source, "real_camera", False)), None)
        if camera is not None:
            add_camera_information(section, self._information_options(), camera)
        for source in sources:
            add_camera_settings(section, self._information_options(), source)

    def _notify_programs_started(self) -> None:
        """Tell programs that want to know (an optional `_device_started()`) that the device is connected, so they can publish.

        A program cannot publish before this: it is not attached to a started device until now. Never raises."""
        for program in list(self._programs.values()):
            hook = getattr(program, "_device_started", None)
            if callable(hook):
                try:
                    hook()
                except Exception:
                    logger.debug("program %r could not start", program.id, exc_info=True)

    def _publish_stream_ids(self) -> None:
        """Tell the dashboard which stream each Video output carries: its `streamId` runtime value.

        Only when direct connections are on and a source is attached, so a Video widget with nothing behind
        it publishes nothing and the page says there is no camera."""
        if self._peers is None:
            return
        for program in self._programs.values():
            for stream_id in getattr(program, "video_sources", lambda: {})():
                widget_id = stream_id.split(".", 1)[1] if "." in stream_id else stream_id
                widget = program.widget(widget_id) if hasattr(program, "widget") else None
                if callable(getattr(widget, "attach_stream", None)):
                    widget.attach_stream(stream_id)  # a Camera: the stream id is one part of its state
                    continue
                publish = getattr(program, "_publish_runtime", None)
                if callable(publish):
                    publish(widget_id, value={"streamId": stream_id})

    def stop(self) -> None:
        """Stop the receive loop, disconnect the transport, and mark the
        device as stopped.

        Calling
        `stop()` more than once is safe (a no-op after the first call),
        and `stop()` before ever calling `start()` still raises - those
        are different situations, not the same "already stopped" case.

        The transport is disconnected *before* the runtime service is
        told to stop, not after: the receive loop is normally sitting
        inside a blocking `transport.receive()` call, and setting a stop
        flag alone can't interrupt that - only actually closing the
        connection can. Disconnecting first makes that blocking call
        raise immediately, so the loop notices and exits right away and
        `DeviceRuntimeService.stop()`'s `join()` returns quickly instead
        of waiting out its full timeout.
        """
        if not self._ever_started:
            raise RuntimeError(f"device {self.id!r} is not started")
        if not self._started:
            return

        self._started = False

        for program in list(self._programs.values()):
            hook = getattr(program, "_device_stopped", None)
            if callable(hook):
                try:
                    hook()
                except Exception:
                    logger.debug("program %r could not stop cleanly", program.id, exc_info=True)

        disconnect = getattr(self._transport, "disconnect", None)
        if callable(disconnect):
            disconnect()

        if self._runtime_service is not None:
            self._runtime_service.stop()

        if self._peers is not None:
            self._peers.stop()
            self._peers = None

        self._release_device_lock()

    def _start_webrtc(self, built_here: bool) -> None:
        """Turn on direct browser connections, if wanted and possible.

        Never raises: WebRTC is an addition, and a device that cannot have it
        must still run exactly as it always did.
        """
        wanted = self._webrtc_setting if self._webrtc_setting is not None else built_here
        if not wanted or os.environ.get("PIHUB_WEBRTC", "1") == "0":
            return
        try:
            from pihub.webrtc import PeerManager, webrtc_available

            if not webrtc_available():
                logger.warning(
                    "device %r: direct browser connections and live video are off, because the WebRTC library (aiortc) is not available. %s "
                    "Browsers will use the platform's path meanwhile.",
                    self.id,
                    _webrtc_install_hint(),
                )
                return
            peers = PeerManager(
                lambda message: self._transport.send(message.to_json()),
                self._limits,
                device_id=self.id,
                snapshot=self._runtime_snapshot,
                on_command=self.handle_incoming,
                on_uplink=self._on_uplink,
                resolve_file=self._resolve_file,
            )
            sources: Dict[str, Any] = {}
            for program in self._programs.values():
                sources.update(getattr(program, "video_sources", lambda: {})())
            peers.set_video_sources(sources)
            peers.start()
        except Exception:
            logger.exception("device %r: could not start webrtc; browsers will use the relay", self.id)
            return
        self._peers = peers

    def _resolve_file(self, program_id: str, widget_id: str, folder: str, name: str) -> Optional[Path]:
        """The real path a `Files` widget's `folder`/`name` names, or None. Called from the WebRTC thread; a
        `Program` with no such widget (or an older one with no `resolve_file`) answers None, same as "not found"."""
        program = self._programs.get(program_id)
        resolve = getattr(program, "resolve_file", None)
        return resolve(widget_id, folder, name) if callable(resolve) else None

    def _can_reconnect(self) -> bool:
        """Whether the transport can be disconnected and connected again - a
        `WebSocketTransport` can; `ConsoleTransport` has nothing to reconnect."""
        return callable(getattr(self._transport, "connect", None)) and callable(
            getattr(self._transport, "disconnect", None)
        )

    def _announce(self) -> None:
        """Introduce this device to the platform: its manifest, then the latest
        value of every runtime widget. Sent straight to the transport so a
        failure reaches the caller - `send()` deliberately swallows them."""
        manifest = build_device_manifest(self.id, self._programs.values(), collect_fingerprint())
        self._transport.send(manifest.to_json())
        if self._uplink_wanted():  # nothing to replay to a platform nobody is watching through
            for message in list(self._latest_runtime.values()):
                self._transport.send(message.to_json())
        if self._peers is not None:
            # Says what this device can do, and which viewer sessions it still
            # holds, so a reconnect to a restarted platform re-admits them.
            self._peers.announce()

    def _first_connection(self) -> None:
        """Connect for the first time, tolerating an unreachable platform.

        A Pi that boots before its wifi is up, or a server mid-deploy, is not
        an error - the device carries on and connects in the background. A
        platform that *answers and refuses* is different: with a wrong
        secret every retry would fail the same way, so that is raised now,
        while a person may be watching.
        """
        try:
            self._connect_transport()
            self._announce()
        except ConnectionError as error:
            status = getattr(error, "status", None)
            if status in REFUSED_STATUSES:
                from pihub.transport.websocket import DeviceRefusedError  # here: importing transports at module level is circular

                # `from None`: the handshake traceback underneath says nothing a person can act on.
                raise DeviceRefusedError(self.id, status) from None
            logger.warning(
                "device %r could not reach the platform yet (%s); it will keep trying in the background",
                self.id, error,
            )
            self._transport.disconnect()
        else:
            self._connected_since = self._clock()

    def _restore_connection(self, stop_event: threading.Event) -> bool:
        """Reconnect after a loss, retrying until it works or the device is stopped.

        Runs on the receive-loop thread, which is otherwise idle: the loop has
        nothing to receive from a dead connection. Returns True once
        reconnected and re-announced, or False if the device was stopped.
        """
        if stop_event.is_set() or not self._started:
            return False
        self._note_connection_lost()

        started = self._clock()
        attempts = 0
        refused = 0
        logger.warning("device %r lost its connection to the platform; reconnecting", self.id)
        while not stop_event.is_set() and self._started:
            delay = self._backoff.next_delay()
            if stop_event.wait(delay):
                return False
            attempts += 1
            try:
                self._transport.disconnect()
                self._transport.connect()
                self._announce()
            except Exception as error:
                status = getattr(error, "status", None)
                refused = refused + 1 if status in REFUSED_STATUSES else 0
                self._log_retry(attempts, refused, error, delay)
                continue
            if stop_event.is_set():
                self._transport.disconnect()
                return False
            self._connected_since = self._clock()
            logger.warning(
                "device %r reconnected after %d attempt(s), %.0fs offline",
                self.id, attempts, self._clock() - started,
            )
            return True
        return False

    def _note_connection_lost(self) -> None:
        """A connection that lasted long enough was healthy, so the next outage
        starts again from a short delay. One that flapped quickly keeps its
        growing delay: the platform counts a reconnect storm as a violation."""
        if self._connected_since is not None and self._clock() - self._connected_since >= STABLE_AFTER_SECONDS:
            self._backoff.reset()
        self._connected_since = None

    def _log_retry(self, attempts: int, refused: int, error: Exception, delay: float) -> None:
        """Say something useful without filling a log: the first failure, then every tenth."""
        if refused and (refused == 1 or refused % 10 == 0):
            logger.error(
                "device %r: the platform refused this device (HTTP %s). %s Still retrying about every %.0fs.",
                self.id, getattr(error, "status", "?"), REFUSED_ADVICE, delay,
            )
        elif attempts == 1 or attempts % 10 == 0:
            logger.warning(
                "device %r: reconnect attempt %d failed (%s); trying again in about %.0fs",
                self.id, attempts, error, delay,
            )
        else:
            logger.debug("device %r: reconnect attempt %d failed (%s)", self.id, attempts, error)

    def _connect_transport(self) -> None:
        """Connect the transport automatically, if it can be connected and isn't already.

        `Transport` (this module's own minimal protocol) only requires
        `send` - `connect`/`connected` are an optional extra capability a
        concrete transport (like `WebSocketTransport`) may offer, checked
        the same way `add_program()` checks for an optional
        `_bind_device` hook. A transport that reports itself as already
        connected (like `ConsoleTransport`, which starts connected) is
        left alone rather than reconnected.
        """
        connect = getattr(self._transport, "connect", None)
        if callable(connect) and not getattr(self._transport, "connected", True):
            connect()

    def _start_runtime_service(self) -> None:
        """Start the background receive loop, if the transport can receive."""
        receive = getattr(self._transport, "receive", None)
        if not callable(receive):
            return

        if self._runtime_service is None:
            self._runtime_service = DeviceRuntimeService(
                self, self._transport, on_disconnect=self._restore_connection if self._reconnect else None
            )
        self._runtime_service.start()

    def register(
        self,
        token: str,
        *,
        server: str,
        http_post: HttpPost = default_http_post,
        allow_insecure: Optional[bool] = None,
    ) -> DeviceConfig:
        """One-time exchange of a registration token for a device secret.

        Per the Authentication and Device Registration Implementation
        Prompt: this is a separate, earlier bootstrap step from the
        ongoing `RegistrationMessage` sent over the WebSocket on every
        `start()` - it happens once per registration token, over plain
        HTTP, before any WebSocket connection exists. `server` is the
        platform's HTTP base URL (e.g. `"http://localhost:8000"`).

        The device's fingerprint is collected and sent automatically - the caller never
        builds this payload by hand, matching how `start()` already
        collects it automatically for the ongoing registration message.

        `allow_insecure` permits a plain `http://` `server` to anything but
        loopback - `None` (the default) defers to `PIHUB_ALLOW_INSECURE`,
        same as `exchange_registration_token()` itself.

        On success, the returned secret is persisted to this device's
        config file (see `pihub.device_config`) so a later `start()` can
        authenticate automatically without repeating this exchange.
        """
        if self.id is None:
            raise RuntimeError("this device has no id yet: pass Device(id=...) to register with a token")
        fingerprint = collect_fingerprint().to_dict()
        response = exchange_registration_token(
            server, self.id, token, fingerprint, http_post=http_post, allow_insecure=allow_insecure
        )

        ws_server = server.replace("https://", "wss://").replace("http://", "ws://")
        config = DeviceConfig(
            device_id=response["deviceId"],
            device_secret=response["deviceSecret"],
            server=ws_server,
        )
        save_device_config(config, self._config_path)
        return config

    def _run_register_if_needed(self) -> None:
        """The `register_if_needed=True` fallback: interactive
        link-code registration under this device's own `id`, run once, only when `start()` finds no
        saved config at all. Blocks - prints, waits for browser approval - by design: this only runs
        for a human running a program directly at a terminal (see `register_if_needed`'s own
        docstring for why it defaults off). Raises `RuntimeError` if linking fails, expires, or is
        declined, same as any other reason `start()` cannot proceed.
        """
        server = (self._server_setting or os.environ.get(SERVER_ENV, DEFAULT_SERVER)).rstrip("/")
        try:
            require_secure_url(server)
        except InsecureTransportError as error:
            raise RuntimeError(str(error)) from error

        print(f"Device {self.id!r} is not registered yet." if self.id is not None else "This device is not registered yet.")
        try:
            link = request_link_code(server, socket.gethostname(), device_id=self.id)  # no id: the platform assigns one
        except RegistrationError as error:
            raise RuntimeError(f"could not start linking: {error}") from error

        print("Link this device")
        print()
        print(f"Visit:   {link['linkUrl']}")
        print(f"Code:    {link['linkCode']}")
        print()
        print("Waiting for approval...")

        try:
            approved = poll_until_approved(server, link)
        except LinkExpiredError as error:
            raise RuntimeError("the link code expired before it was approved") from error
        except RegistrationError as error:
            raise RuntimeError(str(error)) from error

        owner = approved.get("ownerEmail") or "an account"
        print()
        print(f"Approved by {owner}")
        # Approving a code assigns *permanent* ownership, so a wrong link has to be visible here,
        # not only in a browser - same reasoning `pihub setup` already applies.
        answer = input("Link this device to that account? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise RuntimeError("cancelled - nothing was registered")

        if self.id is None:
            self.id = approved["deviceId"]  # the one the platform assigned
        self.register(approved["registrationToken"], server=server)
        print(f"Registered as {self.id!r}")

    def _build_authenticated_transport(self) -> Optional[Transport]:
        """Build a `WebSocketTransport` from this device's persisted
        config, if any - what makes `device.start()` "authenticate
        automatically using the stored secret".

        The secret travels in an `Authorization: Bearer` header rather
        than in the connection URL: a URL ends up in access logs, proxy logs and
        error reports, and a device secret does not expire on its own.
        `WebSocketTransport` is imported lazily to avoid a circular
        import (`transport/websocket.py` imports `Transport` from this
        module).

        Returns `None` (leaving `_transport` unset, exactly like before
        this prompt) if this device was never registered, or the
        persisted config belongs to a different device id.
        """
        config = load_device_config(self._config_path)
        if config is None:
            return None
        if config.device_id != self.id:
            # Not connecting is correct - that secret belongs to another
            # device - but staying silent left a script that looked like it
            # was running while nothing was ever sent.
            logger.warning(
                "device %r has no connection: the saved registration at %s is for %r. "
                "Use Device() to run as the registered device, or Device(id=%r).",
                self.id, self._config_path, config.device_id, config.device_id,
            )
            return None

        from pihub.transport.websocket import WebSocketTransport

        url = f"{config.server.rstrip('/')}/ws/devices/{config.device_id}"
        return WebSocketTransport(
            url=url, headers={"Authorization": f"Bearer {config.device_secret}"}
        )

    def handle_incoming(self, data: str) -> None:
        """Dispatch a raw envelope received from the transport.

        By the protocol, Device only ever receives
        `command` (Browser -> Device) and `lifecycle` (Platform -> Device)
        messages. Unknown message types are ignored per the protocol's
        backward-compatibility rule; other known types are out of
        direction for Device and are ignored too rather than raising.

        Dispatch uses the naming convention from "Event Bus Integration":
        `lifecycle.<event>` and `command.<widgetId>`.
        """
        envelope = Envelope.from_json(data)
        message = parse_message(envelope)
        if message is None:
            return

        if envelope.message_type == MESSAGE_TYPE_LIFECYCLE:
            assert isinstance(message, LifecycleMessage)
            self.events.publish(f"lifecycle.{message.event}", message.data)
        elif envelope.message_type == MESSAGE_TYPE_COMMAND:
            assert isinstance(message, CommandMessage)
            self.events.publish(f"command.{message.binding.widget_id}", message)
        elif envelope.message_type == MESSAGE_TYPE_SIGNAL:
            assert isinstance(message, SignalMessage)
            if self._peers is not None:
                self._peers.handle_signal(message)

    def simulate_command(
        self,
        program_id: str,
        widget_id: str,
        *,
        action: Optional[str] = None,
        values: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Simulate an inbound `command` message, for local development and tests.

        A developer-convenience helper only - it changes nothing about the
        protocol or the Event Bus. It builds a real `CommandMessage` (via
        the existing `Binding`), serializes it exactly as a transport
        would, and routes it through `handle_incoming()` rather than
        publishing to `events` directly, so it exercises the same parsing
        and dispatch path a real WebRTC/WebSocket/future transport would.
        """
        binding = Binding(device_id=self.id, program_id=program_id, widget_id=widget_id)
        message = CommandMessage(binding=binding, action=action, values=values)
        self.handle_incoming(message.to_json())

    def simulate_lifecycle(self, event: str, **data: Any) -> None:
        """Simulate an inbound `lifecycle` message, for local development and tests.

        Same rationale as `simulate_command()`: builds a real
        `LifecycleMessage` and routes it through `handle_incoming()`
        rather than publishing to `events` directly.
        """
        message = LifecycleMessage(event=event, data=dict(data))
        self.handle_incoming(message.to_json())

    def _uplink_wanted(self) -> bool:
        """Whether runtime values should go to the platform now. Always, unless direct viewers are the only ones watching, or nobody is."""
        peers = self._peers
        return peers is None or peers.uplink_required()

    def _on_uplink(self, required: bool) -> None:
        """The platform has started to need our values (a viewer is connecting, or dropped): bring it up to date, off the WebRTC loop."""
        if required:
            threading.Thread(target=self._send_uplink_snapshot, name="pihub-uplink-snapshot", daemon=True).start()

    def _send_uplink_snapshot(self) -> None:
        """The latest value of everything, to the platform. Each is looked up as it is sent, so a live value sent meanwhile is never overwritten by an older one."""
        for key in list(self._latest_runtime.copy()):
            message = self._latest_runtime.get(key)
            if message is None or self._transport is None:
                continue
            try:
                self._transport.send(message.to_json())
            except Exception:  # not connected: the platform is brought up to date by the reconnect's replay
                logger.debug("device %r: could not send the uplink snapshot", self.id, exc_info=True)
                return

    def _runtime_snapshot(self) -> List[RuntimeMessage]:
        """The latest value of every runtime endpoint, for a viewer that has just connected directly."""
        return list(self._latest_runtime.copy().values())

    def send(self, message: OutgoingMessage) -> None:
        """Send an outbound protocol message through the transport.

        Used internally for the registration manifest on `start()`, and by
        an attached `Program` to deliver `runtime` and `notification`
        messages. A no-op if no transport was configured. While a lost
        connection is being restored (see `reconnect`), messages are dropped
        rather than raised, and the latest value of each runtime widget is
        re-sent once the connection is back.
        """
        if isinstance(message, RuntimeMessage):
            self._latest_runtime[(message.binding.program_id, message.binding.widget_id)] = message
            if self._peers is not None:
                self._peers.publish_runtime(message)  # straight to viewers connected directly; independent of the platform connection
                if not self._uplink_wanted():
                    return  # nobody needs the platform's copy: only notifications, registration and signalling go up (demand-driven uplink)
        if self._transport is None:
            return
        try:
            self._transport.send(message.to_json())
        except ConnectionError:
            if not self._reconnect:
                raise
            # Mid-outage: the platform is unreachable and a reconnect is
            # already under way. Raising here would crash the program's own
            # loop for something it cannot fix. A runtime value is replayed on
            # reconnect; a notification sent right now is the one thing lost.
            logger.debug("device %r: dropped a %s while disconnected", self.id, type(message).__name__)
