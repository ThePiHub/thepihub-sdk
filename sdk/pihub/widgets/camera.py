"""The Camera component : ONE widget that owns a camera's video, its state and
its actions, so the dashboard can draw it as the video with a bar of controls (stream, record, snapshot, full screen).

It is the first of a small, repeatable pattern for a "component" (a widget that owns video, state and named actions together):

* **capabilities** `["display", "command"]`, and properties saying what it offers (`snapshot`, `record`), so the dashboard only shows
  controls that will work;
* **one state value**, published whole whenever any part changes, so the dashboard never shows a combination that was never true;
* **named actions**: commands addressed to the widget itself (`CommandMessage.action`): `snapshot`, `record-start`, `record-stop`,
  `extend`. The SDK handles them; there is no handler to write.

```python
camera = program.output(id="camera", type="Camera")
camera.use(source)                         # live video (as a "Video" output) and the Snapshot action
recorder = camera.recorder(max_seconds=300) # adds the Record action; the program can still call recorder.start()/stop() itself
```

While a recording runs the state is published once a second, so "REC 00:42" and "Recording stops in 30 s" are right to the second on
the dashboard however late a viewer arrives, and whatever the Pi's clock says (it has no real-time clock). Nobody watching, a publish
only updates the device's own copy of the latest value.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from pihub.properties.boolean import BooleanProperty
from pihub.protocol.messages import CAPABILITY_COMMAND, CAPABILITY_DISPLAY, CommandMessage
from pihub.widgets.output import Output

logger = logging.getLogger(__name__)

CAMERA = "Camera"
DEFAULT_SNAPSHOT_DIRECTORY = "~/thepihub-snapshots"
TICK_SECONDS = 1.0

# The actions a Camera answers, as `CommandMessage.action`.
SNAPSHOT, RECORD_START, RECORD_STOP, EXTEND = "snapshot", "record-start", "record-stop", "extend"


@dataclass
class Camera(Output):
    """A camera: `use(source)` for video and snapshots, `recorder(...)` for recording. Made by `program.output(id, type="Camera")`."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_DISPLAY, CAPABILITY_COMMAND]

    snapshot_directory: Path = field(default=Path(DEFAULT_SNAPSHOT_DIRECTORY).expanduser(), init=False, repr=False, compare=False)
    _recorder: Optional[Any] = field(default=None, init=False, repr=False, compare=False)
    _stream_id: Optional[str] = field(default=None, init=False, repr=False, compare=False)
    _last_snapshot: Optional[str] = field(default=None, init=False, repr=False, compare=False)
    _snapshotting: bool = field(default=False, init=False, repr=False, compare=False)
    _storage_percent: Optional[int] = field(default=None, init=False, repr=False, compare=False)
    _started: bool = field(default=False, init=False, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False, compare=False)
    _stop_ticking: Optional[threading.Event] = field(default=None, init=False, repr=False, compare=False)

    # -- the program's side --------------------------------------------------------------------------------------------------

    def use(self, source: Any, *, snapshot_directory: Optional[str] = None) -> "Camera":
        """The camera's video source: live video (started only when a viewer asks, as for a "Video" output) and the Snapshot action,
        which saves a JPEG in `snapshot_directory` (default `~/thepihub-snapshots`)."""
        self.source = source
        if snapshot_directory is not None:
            self.snapshot_directory = Path(snapshot_directory).expanduser()
        self._offer("snapshot")
        return self

    def recorder(self, **options: Any) -> Any:
        """Add recording: a `Recorder` of this camera's source (the options are `Recorder`'s), whose notices go to this camera's section.

        The dashboard's Record button starts and stops it; the program can too (`recorder.start(reason=...)`), and the camera's state
        follows whichever did. Its settings appear in Device Information's "Video" group. One recorder per camera.
        """
        if self.source is None:
            raise RuntimeError(f"camera {self.id!r}: call use(source) before recorder()")
        if self._recorder is not None:
            raise RuntimeError(f"camera {self.id!r} already has a recorder")
        from pihub.video.recording import Recorder

        options.setdefault("notify", self._host)
        recorder = Recorder(self.source, **options)
        recorder.on_change(lambda _recorder: self._changed())
        recorder.on_saved(lambda _recording: self._storage_changed())
        self._recorder = recorder
        self._offer("record")
        return recorder

    def recorder_if_possible(self, **options: Any) -> Optional[Any]:
        """`recorder(...)` if this camera's source can be recorded, else nothing (returns `None`), so the same lines work on every machine.

        Recording needs an ENCODED source (the Pi's hardware encoder, libx264 on the CPU through `SoftwareEncodedCameraSource`, or the encoded test pattern). A Pi camera whose raw
        frames `aiortc` encodes (`PiCameraSource`, the fallback when libx264 is missing) can stream and take snapshots but not record, and then there is no Record button. `recorder()`
        itself stays strict: it raises for such a source.
        """
        if self.source is None:
            raise RuntimeError(f"camera {self.id!r}: call use(source) before recorder_if_possible()")
        if not getattr(self.source, "encoded", False):
            return None
        return self.recorder(**options)

    def _offer(self, name: str) -> None:
        if not any(prop.name == name for prop in self.properties):
            self.properties.append(BooleanProperty(name, default=True))

    # -- the state -----------------------------------------------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """The camera's one runtime value."""
        recorder = self._recorder
        current = recorder.current if recorder is not None else None
        seconds_left = recorder.seconds_left() if recorder is not None else None
        return {
            "streamId": self._stream_id,
            "recording": current is not None,
            "recordingReason": current.reason if current is not None else None,
            "recordingStartedAt": current.started_at if current is not None else None,
            "recordingSeconds": _elapsed(recorder),  # since start() (the trigger): pre-roll is extra, before it
            "recordingSecondsLeft": round(seconds_left, 1) if seconds_left is not None else None,
            "warnBeforeEndSeconds": recorder.warn_before_end_seconds if recorder is not None else None,
            "storageUsedPercent": self._storage_percent,
            "lastSnapshot": self._last_snapshot,
            "snapshotting": self._snapshotting,
        }

    def publish(self) -> None:
        """Publish the state now (only once the device has started: before that there is nowhere to send it)."""
        if not self._started or self._host is None:
            return
        try:
            self.set(self.state())
        except Exception:
            logger.debug("camera %r could not publish its state", self.id, exc_info=True)

    def _changed(self) -> None:
        self._tick_while_recording()
        self.publish()

    def _storage_changed(self) -> None:
        recorder = self._recorder
        if recorder is not None:
            try:
                self._storage_percent = recorder.store.used_percent()
            except OSError:
                self._storage_percent = None
        self.publish()

    def _tick_while_recording(self) -> None:
        """While a recording runs, publish once a second; stop when it ends."""
        recording = self._recorder is not None and self._recorder.recording
        with self._lock:
            if recording and self._stop_ticking is None and self._started:
                stop = threading.Event()
                self._stop_ticking = stop
                threading.Thread(target=self._tick, args=(stop,), daemon=True, name=f"camera-{self.id}").start()
            elif not recording and self._stop_ticking is not None:
                self._stop_ticking.set()
                self._stop_ticking = None

    def _tick(self, stop: threading.Event) -> None:
        while not stop.wait(TICK_SECONDS):
            if self._recorder is None or not self._recorder.recording:
                return
            self.publish()

    # -- the device's side ---------------------------------------------------------------------------------------------------

    def attach_stream(self, stream_id: str) -> None:
        """`Device` calls this when live video is available (direct connections are on): the dashboard's Stream control then works."""
        self._stream_id = stream_id
        self.publish()

    def device_started(self) -> None:
        self._started = True
        self._storage_changed()  # publishes
        self._tick_while_recording()

    def device_stopped(self) -> None:
        self._started = False
        with self._lock:
            if self._stop_ticking is not None:
                self._stop_ticking.set()
                self._stop_ticking = None

    def handle(self, command: CommandMessage) -> None:
        """An action from the dashboard. Returns at once: anything slow (opening the camera) runs on a thread of its own."""
        action = command.action
        if action == SNAPSHOT and self.source is not None:
            self._snapshot()
        elif action == RECORD_START and self._recorder is not None:
            threading.Thread(target=self._record_start, daemon=True, name=f"camera-{self.id}-record").start()
        elif action == RECORD_STOP and self._recorder is not None:
            self._recorder.stop(wait=False)
        elif action == EXTEND and self._recorder is not None:
            self._recorder.extend()
        else:
            logger.debug("camera %r: ignoring action %r", self.id, action)

    def _record_start(self) -> None:
        from pihub.video.sources import CameraUnavailable

        try:
            self._recorder.start(reason="dashboard")
        except (CameraUnavailable, OSError) as error:
            self._notify_warning(f"Recording did not start: {error}")

    def _snapshot(self) -> None:
        with self._lock:
            if self._snapshotting:
                return  # one at a time: a second press while the first is being taken does nothing
            self._snapshotting = True
        self.publish()
        threading.Thread(target=self._take_snapshot, daemon=True, name=f"camera-{self.id}-snapshot").start()

    def _take_snapshot(self) -> None:
        from pihub.video.sources import CameraUnavailable
        from pihub.video.stills import capture_still

        try:
            path = capture_still(self.source, self.snapshot_directory, prefix=self.id)
        except CameraUnavailable as error:
            self._notify("error", f"Snapshot failed: {error}")
        else:
            self._last_snapshot = path.name
            self._notify("info", f"Snapshot saved on the device: {path.name}")
        finally:
            self._snapshotting = False
            self.publish()

    def _notify_warning(self, message: str) -> None:
        self._notify("warning", message)

    def _notify(self, severity: str, message: str) -> None:
        send = getattr(self._host, {"info": "info", "warning": "warning", "error": "error"}[severity], None)
        if callable(send):
            try:
                send(message)
            except Exception:
                logger.debug("camera %r could not send a notice", self.id, exc_info=True)


def _elapsed(recorder: Optional[Any]) -> Optional[float]:
    """Seconds since the current recording started, or None."""
    if recorder is None or recorder.current is None:
        return None
    return round(recorder.elapsed_seconds(), 1)
