"""Video sources: where frames come from.

`read()` blocks until the next frame is due and returns an `av.VideoFrame` (YUV420).
It is called from a worker thread, never from the event loop, so a slow camera cannot
stall the connection.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

DEFAULT_SIZE = (640, 480)
DEFAULT_FPS = 15
# A camera that has just started often gives dark frames while auto-exposure settles.
SETTLE_FRAMES = 6
# The sizes the Resolution setting offers (the Pi's hardware encoder handles up to 1080p). Measured cost per Pi model is still to do.
RESOLUTIONS: Tuple[Tuple[int, int], ...] = ((320, 240), (640, 480), (800, 600), (1280, 720), (1920, 1080))
FPS_RANGE = (1, 30)  # what the Frame rate setting offers (a source itself accepts up to 60). At a low rate the camera is slow to show its first picture (see hardware.py)
CHANGE_PROMPT = "Camera settings changed. Stop and start the camera to apply."  # sent as a WARNING: the user has to act, so the dashboard asks for it to be acknowledged
CAMERA_GROUP = "Camera"  # the dashboard group for the camera's components (its sensor in `device_information` uses the same name)


logger = logging.getLogger(__name__)

PI_MODEL_FILE = "/proc/device-tree/model"


def running_on_a_pi() -> bool:
    """Whether this machine is a Raspberry Pi (its device tree says so). Used to tell a Pi that cannot use its camera (a problem to report) from a computer with no camera at all (a test pattern is fine)."""
    try:
        with open(PI_MODEL_FILE, "rb") as model:
            return b"Raspberry Pi" in model.read()
    except OSError:
        return False


class CameraUnavailable(RuntimeError):
    """The camera (or the library that drives it) cannot be used. The message says what to do."""


def import_picamera2() -> Any:
    """`picamera2`'s camera, hardware H.264 encoder and output base class, imported lazily.

    `picamera2` is a system package on Raspberry Pi OS (`sudo apt install python3-picamera2`), not something pip
    installs, so a virtual environment made without `--system-site-packages` cannot see it. This looks in the
    system location before giving up, and then says what to do.
    """
    import types

    def load() -> Any:
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import Output

        try:
            from picamera2.encoders import LibavH264Encoder  # libx264 on the CPU: what a Pi with no hardware encoder (a Pi 5) uses
        except ImportError:  # an older picamera2
            LibavH264Encoder = None
        return types.SimpleNamespace(Picamera2=Picamera2, H264Encoder=H264Encoder, LibavH264Encoder=LibavH264Encoder, Output=Output)

    try:
        return load()
    except ImportError:
        system = "/usr/lib/python3/dist-packages"
        if system not in sys.path:
            sys.path.append(system)
        try:
            return load()
        except ImportError as error:
            raise CameraUnavailable(
                "picamera2 is not installed: on Raspberry Pi OS run `sudo apt install python3-picamera2`, "
                "and make sure this program's Python can see system packages"
            ) from error


def new_camera(Picamera2: Any, tuning: Optional[str] = None) -> Any:
    """A `Picamera2`, with a libcamera tuning: the one named, else the one saved by `pihub camera setup`, else the camera's own default.

    `tuning=None` (the default) means "whatever is saved on this machine"; `tuning=""` means the camera's own default, whatever is saved.

    The default tuning assumes a camera WITH an infrared filter. A NoIR module has none, and under that default its picture comes out
    strongly pink (measured on a Zero W with a NoIR Camera Module v2: red 5.6x green). Its own tuning, e.g. `imx219_noir`, is
    near neutral. A camera cannot tell the software which kind it is, so this is a setting. `tuning` is a libcamera tuning file:
    a name (`imx219_noir`, `.json` optional, looked up where libcamera installs them) or a path to one.
    """
    if tuning is None:
        from pihub.camera_config import load_camera_tuning

        tuning = load_camera_tuning()
    if not tuning:
        return Picamera2()
    directory, filename = os.path.split(tuning if tuning.endswith(".json") else tuning + ".json")
    return Picamera2(tuning=Picamera2.load_tuning_file(filename, directory or None))


def close_quietly(camera: Any) -> None:
    """Close a `Picamera2` that was created but never made it into `self._camera`, so a failed open does not leave it holding the device."""
    if camera is None:
        return
    try:
        camera.close()
    except Exception:  # a camera that is already gone, or never started
        logger.debug("closing a camera that failed to open raised", exc_info=True)


# How the camera's picture is turned, as the Orientation setting offers it: label -> (hflip, vflip). The Pi's image processor does flips for free and cannot
# turn a picture a quarter of the way round: a request with a transpose comes back as a flip or a 180 degree rotation (measured on a Pi Zero W, 2026-10-06), so
# 90 and 270 degrees are deliberately NOT offered. Rotating 180 degrees is both flips.
ORIENTATIONS = {
    "Normal": (False, False),
    "Rotate 180°": (True, True),
    "Mirror": (True, False),
    "Flip": (False, True),
}
DEFAULT_ORIENTATION = "Normal"


def make_transform(orientation: str) -> Any:
    """The libcamera `Transform` for an orientation. Only called for one other than Normal, so a camera left as it is never touches libcamera for this."""
    try:
        from libcamera import Transform
    except ImportError as error:
        raise CameraUnavailable(f"cannot turn the picture: libcamera's Python module is not available ({error})") from error
    hflip, vflip = ORIENTATIONS[orientation]
    return Transform(hflip=int(hflip), vflip=int(vflip))


# The size of the small greyscale picture `grab_motion_frame()` returns (width, height): enough to see a person or an animal move, small enough to compare cheaply.
MOTION_FRAME_SIZE = (160, 120)


class VideoSource:
    """Base class. A source has a size and a frame rate, and is started and stopped by whoever needs it.

    **Starting is counted.** A live video stream and a snapshot can both want the camera at once, and only one
    thing can hold a Pi camera. So `start()` opens the camera for the first user and just shares it with any
    later one; `stop()` closes it only when the last user has stopped. Subclasses implement `_open()`,
    `_close()` and `_read()`; callers use `start()`, `stop()` and `read()`.

    **Raw or encoded.** By default a source yields raw frames (`av.VideoFrame`) and `aiortc` encodes them in software
    (H.264): simple, but about 57% of a Pi 3A+ for a lit 640x480 scene. A source with `encoded = True` yields H.264
    packets (`av.Packet`, Annex B) that something else has already encoded, such as the Pi's hardware encoder, and
    `aiortc` sends them without re-encoding (about 4%). Either way the rest of the stack is the same.
    """

    encoded = False
    description = "a video source"  # what `best_camera_source()` reports it chose, in a few words
    real_camera = False  # True for a source that is a physical camera (its colours mean something), not a test pattern
    noir_setting = False  # True once the "Camera is NoIR" toggle has been added to the dashboard for this source (see `add_settings`)
    max_pixels: Optional[int] = None  # the largest picture the Resolution setting offers (None: every size in RESOLUTIONS)
    orientation: str = DEFAULT_ORIENTATION  # a key of ORIENTATIONS; only a real camera (a Pi camera source) turns its picture

    width: int
    height: int
    fps: int

    @property
    def camera(self) -> Optional[Any]:
        """The camera behind this source (a `Picamera2` for a Pi camera) while the source is running, else None: always None for a
        source with no camera, such as a test pattern.

        For a tinkerer who needs something the SDK does not offer: controls, metadata, a still in another mode. The source still owns
        it, so do not stop or close it, and do not change the stream's size or format. Set anything that must hold on every open
        in `on_open`: the camera closes when its last user stops, and `camera` is a new object the next time it opens.
        """
        return getattr(self, "_camera", None)

    def __init__(self, size: Optional[Tuple[int, int]] = None, fps: Optional[int] = None) -> None:
        # A value given here is EXPLICIT: it wins over a value saved from the dashboard (see `add_settings`).
        self.explicit: Set[str] = set()
        if size is not None:
            self.explicit.add("size")
        if fps is not None:
            self.explicit.add("fps")
        size = size if size is not None else DEFAULT_SIZE
        fps = fps if fps is not None else DEFAULT_FPS
        if size[0] <= 0 or size[1] <= 0 or size[0] % 2 or size[1] % 2:
            raise ValueError("video size must be positive and even in both directions (YUV420)")
        if not 1 <= fps <= 60:
            raise ValueError("fps must be between 1 and 60")
        self.width, self.height = size
        self.fps = fps
        self.running = False
        self._users = 0
        self._users_lock = threading.Lock()
        self._latest: Optional[Tuple[float, Any]] = None
        self._start_listeners: List[Callable[["VideoSource"], None]] = []
        self._power_listeners: List[Callable[[bool], None]] = []
        self._pending_settings: Dict[str, Any] = {}  # settings changed while the camera was open: they take effect at the next start
        self._reopen_wanted = False  # a change read when the camera opens (the NoIR tuning) is waiting for it to reopen
        self._pending_listeners: List[Callable[[], bool]] = []
        self._packet_listeners: List[Callable[[bytes, bool, Optional[int]], None]] = []
        self.recorders: List[Any] = []  # every `Recorder` of this source adds itself, so Device Information can show its settings

    def add_start_listener(self, listener: Callable[["VideoSource"], None]) -> None:
        """Call `listener(source)` each time the camera OPENS, that is when its first user starts it (not for a second user sharing it).

        It is called on the starting thread, so it must return quickly: start a thread for anything slow.
        """
        self._start_listeners.append(listener)

    def add_power_listener(self, listener: Callable[[bool], None]) -> None:
        """Call `listener(True)` just BEFORE the camera opens, and `listener(False)` once it has closed (or failed to open).

        For something that must be on exactly while the camera is, whoever is using it (a live stream, a snapshot, a recording, pre-roll):
        an infrared light, say, switched on before the first frame so the camera sets its exposure with it. Called with the camera's lock
        held, so it must be quick (setting a GPIO pin is); an exception from it is logged and ignored.
        """
        self._power_listeners.append(listener)

    def add_pending_listener(self, listener: Callable[[], bool]) -> None:
        """Call `listener()` when a change is waiting for the open camera to reopen: when a setting changes while it is open, and
        each time a user lets go of it and it stays open. The listener returns True if it will reopen the camera for the change
        itself, without the user doing anything (a `Recorder` holding the camera for pre-roll does), and must return quickly.
        """
        self._pending_listeners.append(listener)

    @property
    def users(self) -> int:
        """How many users hold the camera now (0: closed)."""
        return self._users

    @property
    def settings_pending(self) -> bool:
        """A change is waiting for the camera to reopen."""
        return bool(self._pending_settings) or self._reopen_wanted

    def _tell_pending(self) -> bool:
        """Tell the pending listeners (never with the camera's lock held). True if one of them will reopen the camera for the change."""
        handled = False
        for listener in list(self._pending_listeners):
            try:
                handled = bool(listener()) or handled
            except Exception:  # a listener must never stop a setting from being kept
                logger.debug("a pending listener raised", exc_info=True)
        return handled

    def reopen_needed(self) -> bool:
        """Something read when the camera opens has changed (the NoIR tuning). Returns True if the user has to stop and start the camera
        for it, False if it is closed (the next start reads it anyway) or something will reopen it without them."""
        with self._users_lock:
            if self._users == 0:
                return False
            self._reopen_wanted = True
        return not self._tell_pending()

    def _power(self, on: bool) -> None:
        for listener in list(self._power_listeners):
            try:
                listener(on)
            except Exception:  # a light must never stop the camera
                logger.debug("a power listener raised", exc_info=True)

    # -- encoded packets, for more than one consumer ---------------------------------------------------------------------

    def add_packet_listener(self, listener: Callable[[bytes, bool, Optional[int]], None]) -> None:
        """Receive every encoded packet as the encoder makes it: `listener(data, keyframe, timestamp_us)`, `data` Annex B H.264.

        Only for an `encoded` source. This is how ONE encode feeds a live stream and a recorder at once: the stream reads packets
        with `read()` as before, and a listener (a recorder) is handed each one as well, whether anyone is streaming or not. The
        timestamp is the camera's own, in microseconds, when the source knows it (else None). A listener is called on the encoder's
        thread, so it must return quickly (queue the packet); an exception from it is logged and ignored. A listener only gets
        packets while the camera is open, so it should hold the camera with `start()` for as long as it needs them.
        """
        if not self.encoded:
            raise ValueError("only an encoded source (a hardware encoder) has packets to listen to")
        self._packet_listeners.append(listener)

    def remove_packet_listener(self, listener: Callable[[bytes, bool, Optional[int]], None]) -> None:
        try:
            self._packet_listeners.remove(listener)
        except ValueError:
            pass

    def _emit_packet(self, data: bytes, keyframe: bool, timestamp_us: Optional[int]) -> None:
        for listener in list(self._packet_listeners):
            try:
                listener(data, keyframe, timestamp_us)
            except Exception:  # a listener must never break the stream
                logger.debug("a packet listener raised", exc_info=True)

    def start(self) -> None:
        """Acquire the camera (shared with any other user). May raise `CameraUnavailable`."""
        with self._users_lock:
            if self._users == 0:
                for name, value in self._pending_settings.items():  # settings changed while it was closed or open take effect now
                    self._assign(name, value)
                self._pending_settings.clear()
                self._reopen_wanted = False
                self._power(True)
                try:
                    self._open()  # raises before anything is counted, so a failed start holds nothing
                except BaseException:
                    self._power(False)
                    raise
                self._latest = None
                opened = True
            else:
                opened = False
            self._users += 1
            self.running = True
        if opened:
            for listener in list(self._start_listeners):
                try:
                    listener(self)
                except Exception:  # a listener must never stop the camera from starting
                    logger.debug("a start listener raised", exc_info=True)

    # -- settings the dashboard can change -----------------------------------------------------------------------------

    def apply_setting(self, name: str, value: Any) -> bool:
        """Change `size`, `fps` (or, for a hardware encoder, `bitrate`). Takes effect at the NEXT start: a camera that is open keeps
        what it opened with. Returns True if the user has to stop and start the camera for it; False if it is already in effect, or
        something holding the camera will reopen it for the change (see `add_pending_listener`)."""
        with self._users_lock:
            if self._users == 0:
                self._assign(name, value)
                return False
            self._pending_settings[name] = value
        return not self._tell_pending()

    def _assign(self, name: str, value: Any) -> None:
        if name == "size":
            self.width, self.height = value
        elif name == "fps":
            self.fps = int(value)
        elif name == "orientation":
            if value not in ORIENTATIONS:
                raise ValueError(f"orientation must be one of {', '.join(ORIENTATIONS)}")
            self.orientation = value
        else:
            raise ValueError(f"a video source has no setting {name!r}")

    def _set_orientation(self, orientation: Optional[str]) -> None:
        """A constructor value is EXPLICIT (it wins over a saved one, as for size and fps); None leaves the default for the saved setting to replace."""
        if orientation is None:
            return
        if orientation not in ORIENTATIONS:
            raise ValueError(f"orientation must be one of {', '.join(ORIENTATIONS)}")
        self.explicit.add("orientation")
        self.orientation = orientation

    def camera_transform(self) -> dict:
        """What to add to the camera's configuration to turn the picture: `{"transform": ...}`, or nothing at all for Normal."""
        return {} if self.orientation == DEFAULT_ORIENTATION else {"transform": make_transform(self.orientation)}

    def add_settings(self, section: Any, *, sensor: Optional[str] = None) -> None:
        """Add this source's settable components to `section` (Device Information), with the same `section.setting(...)` call any device uses.

        Resolution and Frame rate always; a hardware encoder adds Bitrate; a real camera whose sensor has a NoIR tuning installed adds "Camera is NoIR"
        (`sensor` is its name). A component the section had removed on purpose, or already has, is left alone.

        **Precedence: a value passed to the constructor, then the saved setting, then the default.** A constructor value is shown as the current
        value and is not read from or written to the saved settings, so a dashboard change to it lasts until the program restarts. Every change is
        applied at the next camera start; while the camera is open the user is told to stop and start it, unless the only thing holding it
        is a recorder armed with pre-roll, which reopens it for the change itself.
        """
        options = [f"{w}x{h}" for w, h in RESOLUTIONS if self.max_pixels is None or w * h <= self.max_pixels]
        current = f"{self.width}x{self.height}"
        if current not in options:
            options.append(current)  # a size given in code is always offered
        self._add_setting(
            section, "camera-resolution", "size", "Select", "Resolution", current, options=options,
            convert=lambda text: tuple(int(part) for part in text.split("x")),
        )
        self._add_setting(
            section, "camera-frame-rate", "fps", "Slider", "Frame rate", self.fps, units="fps",
            minimum=FPS_RANGE[0], maximum=max(FPS_RANGE[1], self.fps), step=1,
        )
        if self.real_camera:  # a test pattern has no camera to turn
            self._add_setting(
                section, "camera-orientation", "orientation", "Select", "Orientation", self.orientation, options=list(ORIENTATIONS),
            )
        self._add_noir_setting(section, sensor)

    def _add_setting(self, section: Any, id: str, name: str, type: str, title: str, default: Any, *, convert: Optional[Callable[[Any], Any]] = None, **limits: Any) -> Any:
        if section.is_excluded(id) or section.widget(id) is not None:
            return None
        shown = convert or (lambda value: value)

        def apply(value: Any) -> None:
            waiting = self.apply_setting(name, shown(value))
            if waiting:
                try:
                    section.warning(CHANGE_PROMPT)
                except Exception:  # not connected right now: the change is kept and applies at the next start anyway
                    logger.debug("could not send the camera settings prompt", exc_info=True)

        widget = section.setting(id, type, default=default, title=title, apply=apply, persist=name not in self.explicit, group=CAMERA_GROUP, **limits)
        self.apply_setting(name, shown(widget.value))  # the saved value, if there is one, is what the next start uses
        return widget

    def _add_noir_setting(self, section: Any, sensor: Optional[str]) -> None:
        """"Camera is NoIR": only for a real camera whose sensor has a `<sensor>_noir` tuning installed, and not when a tuning was given in code."""
        id = "camera-noir"
        if not self.real_camera or not sensor or getattr(self, "tuning", None) is not None:
            return
        if section.is_excluded(id) or section.widget(id) is not None:
            return
        from pihub.camera_config import CameraTuningStore, noir_tuning_for

        noir = noir_tuning_for(sensor)
        if noir is None:
            return

        def apply(_value: Any) -> None:
            if self.reopen_needed():  # the tuning is read when the camera opens
                try:
                    section.warning(CHANGE_PROMPT)
                except Exception:
                    logger.debug("could not send the camera settings prompt", exc_info=True)

        section.setting(id, "Toggle", default=False, title="Camera is NoIR", apply=apply, store=CameraTuningStore(noir), group=CAMERA_GROUP)
        self.noir_setting = True

    def stop(self) -> None:
        """Release the camera. It closes when the last user stops, so nothing runs while nobody needs it."""
        with self._users_lock:
            if self._users == 0:
                return
            self._users -= 1
            still_open = self._users > 0
            if not still_open:
                self.running = False
                self._latest = None
                try:
                    self._close()
                finally:
                    self._power(False)
        if still_open and self.settings_pending:  # a change was waiting for this user: whoever is left may reopen the camera for it
            self._tell_pending()

    def read(self) -> Any:  # -> av.VideoFrame, or av.Packet when `encoded`
        """The next frame or packet (blocking). A raw frame is remembered, so a snapshot can reuse a live stream's."""
        item = self._read()
        if not self.encoded:
            self._latest = (time.monotonic(), item)
        return item

    def grab_still(self) -> Any:  # -> av.VideoFrame
        """A raw picture, from a source that is already started (`capture_still` starts and stops it around this).

        A live stream's own latest frame if there is a fresh one, else a frame read after letting exposure settle.
        A source whose live output is already encoded overrides this: an encoded packet is not a picture.
        """
        frame = self.latest_frame()
        if frame is not None:
            return frame
        for _ in range(SETTLE_FRAMES):
            frame = self.read()
        return frame

    def grab_motion_frame(self) -> Optional[Any]:
        """A small greyscale picture for detecting movement by comparing one with the last, or None if this source does not offer one.

        A 2-D `numpy` array of `uint8` brightness values, `MOTION_FRAME_SIZE` pixels (width, height), from a source that is already started.
        It is cheap on purpose: a Pi camera makes it beside the video (see `HardwareCameraSource(motion_frames=True)`), so asking for one
        four times a second costs the CPU a few per cent and does not slow the video. Only a source that was asked for these returns one;
        every other returns None.
        """
        return None

    def request_keyframe(self) -> None:
        """Ask an encoded source for a keyframe now (a browser lost its picture). Raw sources have nothing to do."""

    def latest_frame(self, max_age: float = 2.0) -> Optional[Any]:
        """The most recent frame if the source is running and it is fresh, else None."""
        latest = self._latest
        if self.running and latest is not None and time.monotonic() - latest[0] <= max_age:
            return latest[1]
        return None

    def _open(self) -> None:
        pass

    def _close(self) -> None:
        pass

    def _read(self) -> Any:  # -> av.VideoFrame
        raise NotImplementedError


class TestPatternSource(VideoSource):
    """A moving test pattern: needs no camera, no numpy and no drivers. For tests, demos, and a board with no camera.

    Vertical bars sweep across a gradient, so a person looking at the video can tell it is live
    and a test can tell that consecutive frames differ.
    """

    __test__ = False  # not a pytest test class, despite the name
    description = "a moving test pattern, not a camera"

    def __init__(self, size: Optional[Tuple[int, int]] = None, fps: Optional[int] = None) -> None:
        super().__init__(size, fps)
        self._frame_number = 0
        self._next_due = 0.0

    def _open(self) -> None:
        self._frame_number = 0
        self._next_due = time.monotonic()

    def _read(self) -> Any:
        import av

        if not self.running:
            raise CameraUnavailable("the test pattern source is not running")
        return self._next_pattern_frame()

    def grab_motion_frame(self) -> Optional[Any]:
        """The pattern's brightness at the current frame, `MOTION_FRAME_SIZE` pixels: the moving bar shows up as movement, so a program can be tried with no camera."""
        import numpy as np

        width, height = MOTION_FRAME_SIZE
        row = np.arange(width, dtype=np.int32) * 200 // width + 30
        shift = (self._frame_number * 8) % self.width * width // self.width
        row[shift : min(shift + 48 * width // self.width, width)] = 235
        return np.tile(row.astype(np.uint8), (height, 1))

    def _next_pattern_frame(self) -> Any:
        import av

        # Pace like a camera does: one frame every 1/fps seconds.
        delay = self._next_due - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._next_due = max(self._next_due + 1.0 / self.fps, time.monotonic())

        width, height, n = self.width, self.height, self._frame_number
        self._frame_number += 1
        shift = (n * 8) % width
        # One row of luma: a horizontal gradient, plus a bright bar that moves right.
        row = bytearray((x * 200 // width) + 30 for x in range(width))
        for x in range(shift, min(shift + 48, width)):
            row[x] = 235
        frame = av.VideoFrame(width, height, "yuv420p")
        frame.planes[0].update(bytes(row) * height)
        chroma = bytes([128]) * (width // 2 * (height // 2))
        frame.planes[1].update(chroma)
        frame.planes[2].update(chroma)
        return frame


class PiCameraSource(VideoSource):
    real_camera = True
    description = "software H.264: the Pi's camera, encoded on the CPU"

    """A Raspberry Pi camera, through `picamera2` (the libcamera stack).

    `picamera2` is a system package on Raspberry Pi OS (`sudo apt install python3-picamera2`), not
    something pip installs, so it is imported here and not at module level. A virtual environment
    made without `--system-site-packages` cannot see it; this looks in the system location before
    giving up, and then says what to do.
    """

    max_pixels = 640 * 480  # software H.264 on a Pi: about 57% of a Pi 3A+ at 640x480, so nothing larger is offered

    def __init__(
        self,
        size: Optional[Tuple[int, int]] = None,
        fps: Optional[int] = None,
        tuning: Optional[str] = None,
        on_open: Optional[Callable[[Any], None]] = None,
        orientation: Optional[str] = None,
    ) -> None:
        super().__init__(size, fps)
        self._set_orientation(orientation)
        self.tuning = tuning  # None: the saved setting (`pihub camera setup`); or a libcamera tuning, e.g. "imx219_noir" (see `new_camera`)
        # `on_open(camera)`: called with the `Picamera2` each time the camera opens, after it is configured and before it starts (see `camera`).
        # Do not call `start()` or `stop()` on this source from it. If it raises, the camera is closed and `start()` raises `CameraUnavailable`.
        self.on_open = on_open
        self._camera: Optional[Any] = None

    @staticmethod
    def available() -> bool:
        """Whether the camera library is here and it lists at least one camera. What a Pi 5 (no hardware encoder) needs: it can still use its camera, encoded on the CPU."""
        try:
            return len(import_picamera2().Picamera2.global_camera_info()) > 0
        except Exception:  # no library, no libcamera, or nothing attached
            return False

    @staticmethod
    def _import() -> Any:
        return import_picamera2().Picamera2

    def _open(self) -> None:
        if self._camera is not None:
            return
        Picamera2 = self._import()
        camera = None
        try:
            camera = new_camera(Picamera2, self.tuning)
            camera.configure(
                camera.create_video_configuration(
                    main={"size": (self.width, self.height), "format": "YUV420"},
                    controls={"FrameRate": self.fps},
                    **self.camera_transform(),
                )
            )
            if self.on_open is not None:
                self.on_open(camera)
            camera.start()
        except Exception as error:  # no camera attached, another program holds it, or `on_open` raised
            close_quietly(camera)
            raise CameraUnavailable(f"could not start the camera: {error}") from error
        self._camera = camera

    def _close(self) -> None:
        camera, self._camera = self._camera, None
        if camera is not None:
            try:
                camera.stop()
                camera.close()
            except Exception:  # a camera that is already gone
                pass

    def _read(self) -> Any:
        import av

        camera = self._camera
        if camera is None:
            raise CameraUnavailable("the camera is not running")
        return av.VideoFrame.from_ndarray(camera.capture_array("main"), format="yuv420p")


class EncodedTestPatternSource(TestPatternSource):
    """A stand-in for a hardware encoder: the moving test pattern, ALREADY ENCODED as H.264 packets.

    It needs no camera and no Pi, so the whole pre-encoded path (`encoded = True`, no re-encoding by aiortc,
    keyframes on request, packets for a recorder) can be tested anywhere. It encodes with PyAV's libx264 here, in the
    source, which is exactly the thing a hardware source does on the GPU instead. Regular keyframes with repeated
    headers, like the hardware source, and the same shape: an encoder thread of its own, running while the camera is
    open, putting each packet in the stream's buffer and handing it to any packet listener.
    """

    encoded = True

    def __init__(self, size: Optional[Tuple[int, int]] = None, fps: Optional[int] = None) -> None:
        super().__init__(size, fps)
        self._codec: Optional[Any] = None
        self._buffer: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._force_keyframe = False
        self.keyframes_requested = 0

    def _open(self) -> None:
        import fractions

        import av

        from pihub.video.hardware import PacketBuffer

        super()._open()
        codec = av.CodecContext.create("libx264", "w")
        codec.width, codec.height, codec.pix_fmt = self.width, self.height, "yuv420p"
        codec.framerate = fractions.Fraction(self.fps, 1)
        codec.time_base = fractions.Fraction(1, 90000)
        codec.options = {"tune": "zerolatency", "x264-params": f"repeat-headers=1:keyint={self.fps}"}
        codec.profile = "Baseline"
        self._codec = codec
        self._buffer = PacketBuffer(maxsize=max(8, int(self.fps * 2)), on_overflow=self.request_keyframe)
        self._running.set()
        self._thread = threading.Thread(target=self._encode_loop, args=(codec, self._buffer), daemon=True, name="test-pattern-encoder")
        self._thread.start()

    def _close(self) -> None:
        self._running.clear()
        buffer, self._buffer = self._buffer, None
        if buffer is not None:
            buffer.close()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._codec = None

    def _encode_loop(self, codec: Any, buffer: Any) -> None:
        import fractions

        import av

        while self._running.is_set():
            frame = self._pattern_frame()  # paced like a camera
            if frame is None:
                return
            frame.pts = int(self._frame_number * 90000 / self.fps)
            frame.time_base = fractions.Fraction(1, 90000)
            frame.pict_type = av.video.frame.PictureType.I if self._force_keyframe else av.video.frame.PictureType.NONE
            self._force_keyframe = False
            timestamp_us = int(self._frame_number * 1_000_000 / self.fps)
            for packet in codec.encode(frame):
                if not self._running.is_set():
                    return
                data = bytes(packet)
                buffer.put(data, bool(packet.is_keyframe))
                self._emit_packet(data, bool(packet.is_keyframe), timestamp_us)

    def _pattern_frame(self) -> Optional[Any]:
        return self._next_pattern_frame() if self._running.is_set() else None

    def request_keyframe(self) -> None:
        self.keyframes_requested += 1
        self._force_keyframe = True

    def _read(self) -> Any:
        import queue

        import av

        buffer = self._buffer
        if buffer is None:
            raise CameraUnavailable("the encoded test source is not running")
        try:
            data, _keyframe = buffer.get(max(2.0, 12 / self.fps))
        except queue.Empty:
            raise CameraUnavailable("the encoded test source produced no video") from None
        return av.Packet(data)

    def grab_still(self) -> Any:
        # An encoded packet is not a picture: make a raw one from the same pattern (without disturbing the encoder's pacing).
        return self._still_frame()

    def _still_frame(self) -> Any:
        import av

        width, height = self.width, self.height
        frame = av.VideoFrame(width, height, "yuv420p")
        frame.planes[0].update(bytes((x * 200 // width) + 30 for x in range(width)) * height)
        chroma = bytes([128]) * (width // 2 * (height // 2))
        frame.planes[1].update(chroma)
        frame.planes[2].update(chroma)
        return frame
