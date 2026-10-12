"""Recording video to the device's own storage.

A `Recorder` writes what an encoded camera source makes to files, whether or not anyone is watching, and while live
video streams, from the **same single encode**: it is one more listener of the source's packets
(`VideoSource.add_packet_listener`) and one more counted user of the camera (`start()`/`stop()`).

* **One file per recording, safe on a power cut.** A recording is one fragmented MP4, written for as long as it runs.
  Each keyframe (about one a second) closes the previous fragment, which is then flushed and `fsync`ed, so a file cut
  off at any moment plays up to its last whole second. Proved on a Pi 3A+ and a Pi Zero W with `kill -9`. No re-encoding and no `ffmpeg` command: PyAV muxes the
  encoder's own H.264.
* **Storage that cannot fill the card.** `RecordingStore` deletes the oldest *finished* recording first, whole, until the
  folder is under `max_total_bytes` and the disk has `min_free_bytes` free; the file being written is never touched. If
  that is still not enough, recording stops with a warning rather than fill the card.
* **A safety cap, a warning, and a way to keep going.** A recording stops after `max_seconds` even if nobody calls
  `stop()`. `warn_before_end_seconds` before that, ONE warning is sent; `extend()` pushes the end back by another
  `max_seconds` from that moment (and re-arms the warning). Deciding when a recording *should* stop (motion has ended,
  say) is the calling program's job, not the recorder's.
* **Pre-roll, when armed.** With `pre_roll_seconds` above 0 and `arm()` called, the camera keeps running while nothing is
  recording, feeding a ring of the last few seconds of packets in memory; a recording then begins with them, from a
  keyframe, so the moment before the trigger is in the file. The ring is not fed while a recording runs (those packets
  are being saved already) and starts again the moment it ends. Off by default: being armed keeps the camera encoding.

Nothing here imports `picamera2`. PyAV is imported when a `Recorder` is made, never while a camera is running (see `Recorder.__init__`).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

DEFAULT_DIRECTORY = "~/thepihub-recordings"
DEFAULT_MAX_TOTAL_BYTES = 4 * 1024**3
DEFAULT_MIN_FREE_BYTES = 1 * 1024**3
DEFAULT_MAX_SECONDS = 300.0
DEFAULT_WARN_BEFORE_END_SECONDS = 30.0
# How often a running recording re-checks the storage limits (it can grow past them on its own).
STORAGE_CHECK_SECONDS = 10.0
# How long a recording waits for the camera's next packet before deciding the camera has stopped.
PACKET_TIMEOUT_SECONDS = 5.0
# Packets waiting to be written (the listener runs on the encoder's thread and must never block it): about 20 s at 15 fps.
QUEUE_PACKETS = 300
TIME_BASE = Fraction(1, 90000)
# The longest pre-roll the recorder will keep (the dashboard's slider stops at 15 s): about 30 MB at 4 Mbit/s.
MAX_PRE_ROLL_SECONDS = 60.0
VIDEO_GROUP = "Video"  # the recorder's settings in Device Information, apart from the camera's own ("Camera")
MB = 1024**2
# The rest of the Video group (`Recorder.add_settings`): id, the recorder's attribute, title, units, minimum, maximum, step, and how many of the
# attribute's units one of the slider's is. The byte limits are shown in MB (MiB), so the defaults (4 GiB, 1 GiB) sit on the slider's steps.
LIMIT_SETTINGS = (
    ("video-max-total-mb", "max_total_bytes", "Space for recordings", "MB", 256, 16384, 256, MB),
    ("video-min-free-mb", "min_free_bytes", "Always leave free", "MB", 64, 4096, 64, MB),
    ("video-max-seconds", "max_seconds", "Longest recording", "s", 30, 1800, 30, 1),
    ("video-warn-before-end", "warn_before_end_seconds", "Warn before the end", "s", 0, 120, 5, 1),
)

_NAME = re.compile(r"^rec-(?P<boot>[0-9a-f]+)-(?P<counter>\d+)-\d{8}-\d{6}\.mp4$")

# Why a recording ended, as written in its sidecar and passed to `on_saved`.
STOPPED = "stopped"  # stop() was called
MAX_SECONDS = "max-seconds"  # the safety cap
STORAGE_FULL = "storage-full"  # the disk would have gone below min_free_bytes
CAMERA_STOPPED = "camera-stopped"  # the camera stopped producing video
ERROR = "error"  # the file could not be written


# --- H.264 helpers --------------------------------------------------------------------------------------------------------


def nal_units(data: bytes) -> List[Tuple[int, bytes]]:
    """The NAL units of an Annex B buffer, as `(type, unit including its start code)`. 7 = SPS, 8 = PPS, 5 = IDR, 1 = delta."""
    starts: List[Tuple[int, int]] = []
    i, n = 0, len(data)
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            break
        start = j - 1 if j > 0 and data[j - 1] == 0 else j  # a four-byte start code
        starts.append((start, j + 3))
        i = j + 3
    units = []
    for k, (start, header) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else n
        units.append((data[header] & 0x1F, data[start:end]))
    return units


def parameter_sets(keyframe: bytes) -> bytes:
    """The SPS and PPS carried by a keyframe (the encoder repeats them with every one), or b"" if it has none."""
    return b"".join(unit for kind, unit in nal_units(keyframe) if kind in (7, 8))


# --- the file ---------------------------------------------------------------------------------------------------------------


class FragmentedMp4Writer:
    """Encoded H.264 packets in, ONE fragmented MP4 out, each fragment flushed to the disk as soon as it is complete.

    The file starts at a keyframe (earlier packets are ignored), because its header needs the stream's SPS and PPS and a
    player needs a keyframe to begin. Timestamps come from the camera (microseconds) when given, so a dropped frame
    leaves a gap in time rather than shortening the recording; otherwise frames are spaced at `1/fps`.
    """

    def __init__(self, path: Union[str, Path], fps: int, size: Tuple[int, int], *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.fps = fps
        self.size = size
        self.fsync = fsync
        self.frames = 0
        self.fragments = 0
        self._file: Optional[Any] = None
        self._container: Optional[Any] = None
        self._stream: Optional[Any] = None
        self._first_timestamp: Optional[int] = None
        self._last_pts = -1

    @property
    def started(self) -> bool:
        """Whether the file was begun (a keyframe arrived). Stays True once closed."""
        return self.frames > 0

    @property
    def duration_seconds(self) -> float:
        if self._last_pts < 0:
            return 0.0
        return float((self._last_pts + self._frame_ticks()) * TIME_BASE)

    def _frame_ticks(self) -> int:
        return int(round(1 / (self.fps * TIME_BASE)))

    def write(self, data: bytes, keyframe: bool, timestamp_us: Optional[int] = None) -> bool:
        """Add one packet. Returns True when a fragment was just completed and flushed (at a keyframe)."""
        import av

        if self._container is None:
            if not keyframe:
                return False
            headers = parameter_sets(data)
            if not headers:
                return False  # a keyframe without its headers cannot start a file; the next one will have them
            self._open(headers)
        pts = self._pts(timestamp_us)
        packet = av.Packet(data)
        packet.stream = self._stream
        packet.time_base = TIME_BASE
        packet.pts = packet.dts = pts
        packet.duration = self._frame_ticks()
        packet.is_keyframe = keyframe
        self._container.mux(packet)  # a keyframe makes the muxer write out the fragment before it, there and then
        flushed = False
        if keyframe and self.frames:
            self._flush()  # ... so flush straight after it: that whole fragment is now on the disk (measured: not before this mux)
            flushed = True
        self.frames += 1
        self._last_pts = pts
        return flushed

    def _pts(self, timestamp_us: Optional[int]) -> int:
        if timestamp_us is None:
            pts = self.frames * self._frame_ticks()
        else:
            if self._first_timestamp is None:
                self._first_timestamp = timestamp_us
            pts = int(round((timestamp_us - self._first_timestamp) * 90000 / 1_000_000))
        return max(pts, self._last_pts + 1)  # always increasing, whatever the clock did

    def _open(self, headers: bytes) -> None:
        import av

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "wb")
        try:
            self._container = av.open(
                self._file, mode="w", format="mp4",
                # A header with no sample table up front, then a fragment per keyframe: readable however the file ends.
                options={"movflags": "frag_keyframe+empty_moov+default_base_moof"},
            )
            stream = self._container.add_stream("h264", rate=self.fps)
            stream.width, stream.height = self.size
            stream.time_base = TIME_BASE
            stream.codec_context.extradata = headers
            self._stream = stream
        except Exception:
            self._file.close()
            self._file, self._container = None, None
            raise

    def _flush(self) -> None:
        assert self._file is not None
        self._file.flush()
        if self.fsync:
            os.fsync(self._file.fileno())
        self.fragments += 1

    def close(self) -> None:
        """Finish the file (the last fragment and the index). Safe to call twice, or on a file never started."""
        container, self._container = self._container, None
        if container is not None:
            try:
                container.close()
            finally:
                if self._file is not None:
                    self._file.flush()
                    if self.fsync:
                        os.fsync(self._file.fileno())
                    self._file.close()
                    self._file = None


# --- the folder -------------------------------------------------------------------------------------------------------------


def boot_id() -> str:
    """A short id for this boot of the machine, so recordings can be ordered even when the clock was wrong."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip().replace("-", "")[:8]
    except OSError:
        return uuid.uuid4().hex[:8]


def clock_synchronised() -> Optional[bool]:
    """Whether the system clock is synchronised to a time server (the kernel's own view, whatever NTP program is used).

    None when it cannot be told (not Linux). A Pi has no real-time clock: until it has reached the network, its time is wrong.
    """
    try:
        import ctypes
        import ctypes.util

        class Timex(ctypes.Structure):
            _fields_ = [("modes", ctypes.c_uint), ("offset", ctypes.c_long), ("freq", ctypes.c_long), ("maxerror", ctypes.c_long),
                        ("esterror", ctypes.c_long), ("status", ctypes.c_int), ("pad", ctypes.c_byte * 200)]

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        timex = Timex()
        state = libc.adjtimex(ctypes.byref(timex))
        if state < 0:
            return None
        STA_UNSYNC, TIME_ERROR = 0x0040, 5
        return not (timex.status & STA_UNSYNC) and state != TIME_ERROR
    except Exception:
        return None


@dataclass
class Recording:
    """One recording on the disk: what `on_saved` is given, and what the `.json` beside the file says."""

    path: str
    reason: Optional[str]
    started_at: str  # the wall clock, ISO 8601; see `clock_synced`
    clock_synced: Optional[bool]
    boot_id: str
    counter: int
    width: int
    height: int
    fps: int
    bitrate: Optional[int] = None
    complete: bool = False  # False while it is being written (and for ever, if the device lost power)
    stopped_because: Optional[str] = None
    ended_at: Optional[str] = None
    duration_seconds: float = 0.0
    bytes: int = 0
    frames: int = 0
    extensions: int = 0
    pre_roll_seconds: float = 0.0  # how much of the file is from before start() was called (the trigger)

    @property
    def name(self) -> str:
        return Path(self.path).name

    def sidecar(self) -> Path:
        return Path(self.path).with_suffix(".json")

    def write_sidecar(self, fsync: bool = True) -> None:
        # fsynced like the video: a USB stick pulled 18 s after an un-synced write kept the name but not one byte (2026-09-23)
        target = self.sidecar()
        temporary = target.with_suffix(".json.tmp")
        with open(temporary, "w") as file:
            file.write(json.dumps({key: value for key, value in asdict(self).items() if key != "path"}, indent=2))
            if fsync:
                file.flush()
                os.fsync(file.fileno())
        os.replace(temporary, target)
        if fsync:
            _fsync_directory(target.parent)


class RecordingStore:
    """The recordings folder: names, and the two limits that keep it from filling the card.

    Only files this recorder made (`rec-<boot>-<counter>-<date>-<time>.mp4`, and the `.json` beside each) are ever counted
    or deleted: anything else in the folder is left alone. Nothing is overwritten: every name is new.
    """

    def __init__(
        self,
        directory: Union[str, Path] = DEFAULT_DIRECTORY,
        *,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
        free_bytes: Optional[Callable[[Path], int]] = None,
    ) -> None:
        if max_total_bytes <= 0 or min_free_bytes < 0:
            raise ValueError("max_total_bytes must be positive and min_free_bytes not negative")
        self.directory = Path(directory).expanduser()
        self.max_total_bytes = max_total_bytes
        self.min_free_bytes = min_free_bytes
        self._free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.boot_id = boot_id()

    def recordings(self) -> List[Path]:
        """Every recording file in the folder, oldest first (by when it was last written: when it finished)."""
        if not self.directory.is_dir():
            return []
        files = [path for path in self.directory.iterdir() if _NAME.match(path.name) and path.is_file()]
        return sorted(files, key=lambda path: (path.stat().st_mtime, path.name))

    def used_bytes(self) -> int:
        total = 0
        for path in self.recordings():
            total += _size(path) + _size(path.with_suffix(".json"))
        return total

    def free_bytes(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self._free_bytes(self.directory)

    def new_path(self, now: Optional[float] = None) -> Tuple[Path, int]:
        """A name no recording has had: the next counter after every one in the folder, then the date and time."""
        counters = [int(match.group("counter")) for path in self.recordings() if (match := _NAME.match(path.name))]
        counter = max(counters, default=0) + 1
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        return self.directory / f"rec-{self.boot_id}-{counter:05d}-{stamp}.mp4", counter

    def make_room(self, current: Optional[Path] = None) -> bool:
        """Delete the oldest finished recordings until both limits hold. Never the one being written (`current`).

        Returns False if the disk is still below `min_free_bytes` when there is nothing left to delete.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        candidates = [path for path in self.recordings() if current is None or path.resolve() != Path(current).resolve()]
        while self.used_bytes() > self.max_total_bytes or self.free_bytes() < self.min_free_bytes:
            if not candidates:
                return self.free_bytes() >= self.min_free_bytes
            oldest = candidates.pop(0)
            logger.info("recording: deleting the oldest recording, %s, to keep within the storage limits", oldest.name)
            oldest.unlink(missing_ok=True)
            oldest.with_suffix(".json").unlink(missing_ok=True)
        return True

    def used_percent(self) -> int:
        return min(100, int(round(100 * self.used_bytes() / self.max_total_bytes)))


def _fsync_directory(directory: Path) -> None:
    """Make a rename in `directory` durable. Best effort: not every filesystem lets a directory be fsynced."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# --- the recorder -------------------------------------------------------------------------------------------------------------


def _check_limit(name: str, value: float) -> None:
    if name not in ("max_total_bytes", "min_free_bytes", "max_seconds", "warn_before_end_seconds"):
        raise ValueError(f"a recorder has no setting {name!r}")
    if value < 0 or (value == 0 and name in ("max_total_bytes", "max_seconds")):
        raise ValueError(f"{name} must be positive")


def _check_pre_roll(seconds: float) -> float:
    seconds = float(seconds)
    if not 0 <= seconds <= MAX_PRE_ROLL_SECONDS:
        raise ValueError(f"pre_roll_seconds must be between 0 and {MAX_PRE_ROLL_SECONDS:.0f}")
    return seconds


def _describe(recording: Recording) -> str:
    seconds = int(round(recording.duration_seconds))
    length = f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"
    size = f"{recording.bytes / 1_000_000:.1f} MB" if recording.bytes >= 1_000_000 else f"{recording.bytes / 1000:.0f} kB"
    return f"{recording.name} ({length}, {size})"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


class Recorder:
    """Records an encoded video source to files. `start()` returns at once; the file is written on the recorder's own thread.

    `notify` is anything with `info(message)` and `warning(message)` (a program section), or None to send nothing.
    `clock` and `wall_clock` are for tests.

    `pre_roll_seconds` (0: off) only matters once `arm()` has been called: see the module's notes. A value given here wins
    over one saved from the dashboard's "Pre-roll" setting (`add_settings`); without one, the saved value is used. The same
    goes for the limits (`max_total_bytes`, `min_free_bytes`, `max_seconds`, `warn_before_end_seconds`), and a `store`
    given here makes its two limits count as given.
    """

    def __init__(
        self,
        source: Any,
        directory: Union[str, Path] = DEFAULT_DIRECTORY,
        *,
        pre_roll_seconds: Optional[float] = None,
        max_total_bytes: Optional[int] = None,
        min_free_bytes: Optional[int] = None,
        max_seconds: Optional[float] = None,
        warn_before_end_seconds: Optional[float] = None,
        notify: Optional[Any] = None,
        store: Optional[RecordingStore] = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        fsync: bool = True,
    ) -> None:
        if not getattr(source, "encoded", False):
            raise ValueError("a recorder needs an encoded source (the Pi's hardware encoder, or EncodedTestPatternSource)")
        given = {
            "pre_roll_seconds": pre_roll_seconds, "max_total_bytes": max_total_bytes, "min_free_bytes": min_free_bytes,
            "max_seconds": max_seconds, "warn_before_end_seconds": warn_before_end_seconds,
        }
        self.explicit = {name for name, value in given.items() if value is not None}  # given in code: wins over the dashboard
        if store is not None:
            self.explicit |= {"max_total_bytes", "min_free_bytes"}
        self.pre_roll_seconds = _check_pre_roll(0 if pre_roll_seconds is None else pre_roll_seconds)
        max_seconds = DEFAULT_MAX_SECONDS if max_seconds is None else max_seconds
        warn_before_end_seconds = DEFAULT_WARN_BEFORE_END_SECONDS if warn_before_end_seconds is None else warn_before_end_seconds
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        if warn_before_end_seconds < 0:
            raise ValueError("warn_before_end_seconds cannot be negative")
        # PyAV now, while no camera is open: importing it takes 4.5 s of CPU on a Pi Zero W, and done at the first keyframe
        # (as it once was) it starved picamera2's threads so the camera dropped 2.8 s of frames at the start of every
        # first recording (0.9 s on a Pi 3A+). Measured 2026-09-23.
        import av  # noqa: F401

        self.source = source
        self.store = store or RecordingStore(
            directory,
            max_total_bytes=DEFAULT_MAX_TOTAL_BYTES if max_total_bytes is None else max_total_bytes,
            min_free_bytes=DEFAULT_MIN_FREE_BYTES if min_free_bytes is None else min_free_bytes,
        )
        self.max_seconds = float(max_seconds)
        self.warn_before_end_seconds = float(warn_before_end_seconds)
        self.notify = notify
        self._clock = clock
        self._wall_clock = wall_clock
        self._fsync = fsync
        self._lock = threading.RLock()
        self._packets: "queue.Queue[Tuple[bytes, bool, Optional[int]]]" = queue.Queue(QUEUE_PACKETS)
        self._stop_requested = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[Recording] = None
        self._deadline = 0.0
        self._started_clock = 0.0
        self._warned = False
        self._saved_listeners: List[Callable[[Recording], None]] = []
        self._change_listeners: List[Callable[["Recorder"], None]] = []
        self._settings_listeners: List[Callable[[Any], None]] = []
        self.dropped_packets = 0
        self.last: Optional[Recording] = None
        self._pending: Dict[str, float] = {}  # limits changed during a recording: they apply once it has finished
        # Pre-roll. The ring is a list of GOPs (a keyframe and what follows it), so it can only ever be cut at a keyframe.
        self._armed = False
        self._holding = False  # the camera is held for the ring (armed, with pre-roll on): open even while nothing records
        self._listening = False
        self._route_lock = threading.Lock()  # taken on the encoder's thread for every packet: held only for a moment
        self._to_queue = False  # packets go to the recording being written ...
        self._ring: "deque[Tuple[float, List[Tuple[bytes, bool, Optional[int]]]]]" = deque()  # ... or, if held, to the ring
        recorders = getattr(source, "recorders", None)
        if isinstance(recorders, list):
            recorders.append(self)  # so Device Information can find this recorder's settings (`add_settings`)
        add_pending_listener = getattr(source, "add_pending_listener", None)
        if add_pending_listener is not None:
            add_pending_listener(self._on_settings_pending)  # held for pre-roll, the camera would otherwise never reopen for a change

    # -- the public API --------------------------------------------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._current is not None

    @property
    def current(self) -> Optional[Recording]:
        return self._current

    def elapsed_seconds(self) -> Optional[float]:
        """Seconds since the current recording was started (its trigger; any pre-roll comes before that), or None if nothing is recording."""
        if self._current is None:
            return None
        return max(0.0, self._clock() - self._started_clock)

    def seconds_left(self) -> Optional[float]:
        """Seconds until the safety cap ends the current recording, or None if nothing is recording."""
        if self._current is None:
            return None
        return max(0.0, self._deadline - self._clock())

    def on_saved(self, func: Callable[[Recording], None]) -> Callable[[Recording], None]:
        """Decorator: `func(recording)` is called when a recording has finished and its file is complete."""
        self._saved_listeners.append(func)
        return func

    def on_add_settings(self, func: Callable[[Any], None]) -> Callable[[Any], None]:
        """Decorator: `func(section)` is called when the recorder's settings are added to Device Information, straight after them, so a program
        can put a setting of its own in the Video group beside them (the motion trap's "Keep recording after motion" does)."""
        self._settings_listeners.append(func)
        return func

    def on_change(self, func: Callable[["Recorder"], None]) -> Callable[["Recorder"], None]:
        """`func(recorder)` is called whenever recording starts, stops, or its end moves (for a dashboard's state)."""
        self._change_listeners.append(func)
        return func

    def start(self, reason: Optional[str] = None) -> bool:
        """Start recording. Returns at once: False if already recording (the recorder does not extend on a repeated start).

        Raises `CameraUnavailable` if the camera cannot be opened, and `OSError` if there is no room on the disk.
        """
        with self._lock:
            if self._current is not None:
                return False
            if not self.store.make_room():
                self._warn("Recording did not start: the card is nearly full")
                raise OSError("not enough free space to record")
            path, counter = self.store.new_path(self._wall_clock())
            recording = Recording(
                path=str(path), reason=reason, started_at=_iso(self._wall_clock()), clock_synced=clock_synchronised(),
                boot_id=self.store.boot_id, counter=counter, width=self.source.width, height=self.source.height,
                fps=self.source.fps, bitrate=getattr(self.source, "bitrate", None),
            )
            self.source.start()  # counted: shares a running stream's (or the ring's) camera, or opens it just for this recording
            self._drain_queue()
            self._stop_requested.clear()
            with self._route_lock:  # from this packet on, the recording's: the ring stops being fed, and hands over what it has
                pre_roll, recording.pre_roll_seconds = self._take_ring()
                self._to_queue = True
            recording.pre_roll_seconds = round(recording.pre_roll_seconds, 3)
            self._listen()
            if not pre_roll:
                self.source.request_keyframe()  # a file starts at a keyframe: do not wait up to a second for the regular one
            self._current = recording
            self._started_clock = self._clock()
            self._deadline = self._clock() + self.max_seconds
            self._warned = False
            self._thread = threading.Thread(target=self._run, args=(recording, pre_roll), daemon=True, name="recorder")
            self._thread.start()
        logger.info("recording: started %s (reason: %s)", path.name, reason)
        self._changed()
        return True

    def stop(self, wait: bool = True) -> Optional[Recording]:
        """Stop recording and finish the file. Returns the finished recording (with `wait`), or None if nothing was recording."""
        thread = self._thread
        if self._current is None or thread is None:
            return None
        self._stop_requested.set()
        if wait and thread is not threading.current_thread():
            thread.join()
            return self.last
        return None

    def extend(self, seconds: Optional[float] = None) -> bool:
        """Push the end of the current recording back to `seconds` from now (default: another `max_seconds`). False if not recording."""
        with self._lock:
            if self._current is None:
                return False
            self._deadline = self._clock() + (self.max_seconds if seconds is None else float(seconds))
            self._warned = False
            self._current.extensions += 1
        logger.info("recording: extended; now ends in %.0f s", self.seconds_left() or 0)
        self._changed()
        return True

    def close(self) -> None:
        """Stop any recording, and let go of the camera if it was held for pre-roll (the program is ending)."""
        self.stop(wait=True)
        self.disarm()

    # -- pre-roll --------------------------------------------------------------------------------------------------------

    @property
    def armed(self) -> bool:
        return self._armed

    def arm(self) -> None:
        """Be ready for a trigger: with `pre_roll_seconds` above 0 the camera opens now and stays open, filling the ring.

        With pre-roll at 0 this holds nothing (the camera still opens on `start()`), but a later pre-roll setting takes
        effect at once. Raises `CameraUnavailable` if the camera cannot be opened (the recorder is left armed, holding nothing).
        """
        with self._lock:
            self._armed = True
            self._update_hold()

    def disarm(self) -> None:
        """Stop keeping pre-roll: the ring is emptied and the camera let go (a recording in progress carries on)."""
        with self._lock:
            self._armed = False
            self._update_hold()

    def set_pre_roll(self, seconds: float) -> None:
        """Change how many seconds are kept from before a trigger (0: off). Armed, this opens or lets go of the camera at once.

        A recording in progress is not changed (it already has its pre-roll); the ring, paused while it runs, uses the new value after it.
        """
        seconds = _check_pre_roll(seconds)
        with self._lock:
            self.pre_roll_seconds = seconds
            self._update_hold()

    def add_settings(self, section: Any) -> None:
        """Add the recorder's settings to `section` (Device Information), in the group "Video": Pre-roll, then the limits.

        `Device` calls this for every recorder of a video source it shows the settings of, so a recorder made before
        `device.start()` needs nothing more. A component already there, or removed from the section on purpose, is left alone.
        """
        self._add_pre_roll_setting(section)
        for spec in LIMIT_SETTINGS:
            self._add_limit_setting(section, *spec)
        for listener in self._settings_listeners:
            listener(section)

    def _add_pre_roll_setting(self, section: Any) -> None:
        id = "video-pre-roll"
        if section.is_excluded(id) or section.widget(id) is not None:
            return

        def apply(value: Any) -> None:
            try:
                self.set_pre_roll(float(value))
            except Exception as error:  # the camera would not open: the value is kept and used at the next arm()
                logger.warning("recording: could not apply a pre-roll of %s s: %s", value, error)
                try:
                    section.warning(f"Pre-roll could not start: {error}")
                except Exception:
                    logger.debug("could not send the pre-roll warning", exc_info=True)

        widget = section.setting(
            id, "Slider", default=self.pre_roll_seconds, title="Pre-roll", units="s", minimum=0,
            maximum=max(15, self.pre_roll_seconds), step=1, apply=apply, persist="pre_roll_seconds" not in self.explicit, group=VIDEO_GROUP,
        )
        apply(widget.value)  # the saved value, if there is one

    def _add_limit_setting(self, section: Any, id: str, name: str, title: str, units: str, low: float, high: float, step: float, scale: int) -> None:
        if section.is_excluded(id) or section.widget(id) is not None:
            return
        current = self._limit(name) / scale
        current = int(current) if float(current).is_integer() else current

        def apply(value: Any) -> None:
            self.apply_setting(name, float(value) * scale)

        widget = section.setting(
            id, "Slider", default=current, title=title, units=units, minimum=min(low, current), maximum=max(high, current), step=step,
            apply=apply, persist=name not in self.explicit, group=VIDEO_GROUP,
        )
        apply(widget.value)

    def apply_setting(self, name: str, value: float) -> bool:
        """Change a limit (`max_total_bytes`, `min_free_bytes`, `max_seconds` or `warn_before_end_seconds`) for the NEXT recording.

        A recording in progress keeps the limits it started with. Returns True if the change is waiting for it to finish.
        """
        _check_limit(name, value)
        with self._lock:
            if self._current is not None:
                self._pending[name] = value
                return True
            self._assign_limit(name, value)
            return False

    def _limit(self, name: str) -> float:
        return getattr(self.store, name) if name in ("max_total_bytes", "min_free_bytes") else getattr(self, name)

    def _assign_limit(self, name: str, value: float) -> None:
        if name in ("max_total_bytes", "min_free_bytes"):
            setattr(self.store, name, int(value))
        else:
            setattr(self, name, float(value))

    def _update_hold(self) -> None:
        """Hold the camera for the ring exactly while armed with pre-roll on. Called with `_lock` held."""
        want = self._armed and self.pre_roll_seconds > 0
        if want and not self._holding:
            self.source.start()  # may raise CameraUnavailable: then nothing is held
            with self._route_lock:
                self._ring.clear()
                self._holding = True
            self._listen()
            logger.info("recording: armed, keeping %.0f s of pre-roll", self.pre_roll_seconds)
        elif not want and self._holding:
            self._let_go()
            logger.info("recording: pre-roll off; the camera is no longer held for it")

    def _let_go(self) -> None:
        """Stop holding the camera for the ring. Called with `_lock` held."""
        with self._route_lock:
            self._holding = False
            self._ring.clear()
        self._listen()
        self.source.stop()

    # -- a camera setting changed while pre-roll holds the camera -----------------------------------------------------------
    #
    # Camera settings apply when the camera opens, and held for pre-roll it never closes: found with a real Pi and a real
    # dashboard (S6, 2026-09-23), where a change to 1080p did nothing until Armed was switched off and on. So when only this
    # recorder holds the camera, it reopens it for the change itself: at once if idle (the ring starts again, empty), or once a
    # recording in progress has ended (a change never cuts a recording short). If a live stream holds it too, the user is asked
    # to stop and start it as before, and when the stream lets go this reopens the camera for them.

    def _mine(self) -> bool:
        """Nothing but this recorder holds the camera: the ring, and the recording in progress if there is one."""
        users = getattr(self.source, "users", None)
        return users is not None and users <= (1 if self._holding else 0) + (1 if self._current is not None else 0)

    def _on_settings_pending(self) -> bool:
        """The source has a change waiting for the camera to reopen. True if it will apply without the user doing anything."""
        if not (self._holding or self._current is not None) or not self._mine():
            return False
        if self._holding and self._current is None:
            threading.Thread(target=self._reopen_for_settings, daemon=True, name="recorder-reopen").start()
        return True  # recording: `_finish` reopens it once the recording has ended (or, without pre-roll, the camera closes then)

    def _reopen_for_settings(self) -> None:
        """Close and reopen the camera so a waiting change applies, if only the ring holds it and nothing is recording."""
        with self._lock:
            if not self._holding or self._current is not None or not getattr(self.source, "settings_pending", False) or not self._mine():
                return
            logger.info("recording: reopening the camera for its new settings")
            self._let_go()
            try:
                self._update_hold()
            except Exception as error:  # the camera would not reopen: the recorder stays armed, holding nothing
                logger.warning("recording: the camera did not reopen with its new settings: %s", error)
                self._warn(f"Pre-roll stopped: the camera did not reopen with its new settings ({error})")

    def _listen(self) -> None:
        """Be a packet listener exactly while the ring or a recording wants packets."""
        want = self._holding or self._to_queue
        if want and not self._listening:
            self.source.add_packet_listener(self._on_packet)
            self._listening = True
        elif not want and self._listening:
            self.source.remove_packet_listener(self._on_packet)
            self._listening = False

    def _add_to_ring(self, packet: Tuple[bytes, bool, Optional[int]]) -> None:
        """Called with `_route_lock` held. Keeps at least `pre_roll_seconds`, from a keyframe: up to a GOP (about a second) more.

        A trigger can only come after this packet, so a keyframe at least `pre_roll_seconds` older than it is always enough.
        """
        now = self._clock()
        if packet[1]:
            self._ring.append((now, [packet]))
        elif self._ring:
            self._ring[-1][1].append(packet)
        else:
            return  # the ring starts at a keyframe
        while len(self._ring) >= 2 and self._ring[1][0] <= now - self.pre_roll_seconds:
            self._ring.popleft()  # the next GOP alone already covers the pre-roll

    def _take_ring(self) -> Tuple[List[Tuple[bytes, bool, Optional[int]]], float]:
        """Called with `_route_lock` held: the ring's packets, oldest first, and how many seconds before now they begin. Empties it."""
        if not self._ring:
            return [], 0.0
        seconds = max(0.0, self._clock() - self._ring[0][0])
        packets = [packet for _, gop in self._ring for packet in gop]
        self._ring.clear()
        return packets, seconds

    # -- the writer thread -------------------------------------------------------------------------------------------------

    def _on_packet(self, data: bytes, keyframe: bool, timestamp_us: Optional[int]) -> None:
        with self._route_lock:
            if self._to_queue:
                try:
                    self._packets.put_nowait((data, keyframe, timestamp_us))
                except queue.Full:  # the disk has stalled for many seconds: keep the camera running, lose these frames
                    self.dropped_packets += 1
            elif self._holding:
                self._add_to_ring((data, keyframe, timestamp_us))

    def _drain_queue(self) -> None:
        while True:
            try:
                self._packets.get_nowait()
            except queue.Empty:
                return

    def _run(self, recording: Recording, pre_roll: List[Tuple[bytes, bool, Optional[int]]]) -> None:
        writer = FragmentedMp4Writer(recording.path, recording.fps, (recording.width, recording.height), fsync=self._fsync)
        stopped_because = STOPPED
        next_storage_check = self._clock() + STORAGE_CHECK_SECONDS
        last_packet = self._clock()
        waiting_for_keyframe = True
        try:
            for data, keyframe, timestamp_us in pre_roll:  # from a keyframe, and older than anything in the queue
                waiting_for_keyframe = False
                writer.write(data, keyframe, timestamp_us)
            if writer.started:
                recording.write_sidecar(self._fsync)
            while True:
                if self._stop_requested.is_set():
                    break
                now = self._clock()
                if now >= self._deadline:
                    stopped_because = MAX_SECONDS
                    break
                self._maybe_warn(now)
                if now >= next_storage_check:
                    next_storage_check = now + STORAGE_CHECK_SECONDS
                    if not self.store.make_room(current=Path(recording.path)):
                        stopped_because = STORAGE_FULL
                        break
                try:
                    data, keyframe, timestamp_us = self._packets.get(timeout=0.2)
                except queue.Empty:
                    if self._clock() - last_packet > PACKET_TIMEOUT_SECONDS:
                        stopped_because = CAMERA_STOPPED
                        break
                    continue
                last_packet = self._clock()
                if waiting_for_keyframe and not keyframe:
                    continue
                waiting_for_keyframe = False
                was_started = writer.started
                writer.write(data, keyframe, timestamp_us)
                if writer.started and not was_started:
                    recording.write_sidecar(self._fsync)  # there from the first second: says what the file is, even if power is lost
        except Exception:
            logger.exception("recording: could not write %s", recording.name)
            stopped_because = ERROR
        finally:
            self._finish(recording, writer, stopped_because)

    def _maybe_warn(self, now: float) -> None:
        if self._warned or not self.warn_before_end_seconds:
            return
        if self.warn_before_end_seconds >= self.max_seconds:
            return  # the warning would come as the recording starts: no time left to react, so none is sent
        left = self._deadline - now
        if left <= self.warn_before_end_seconds:
            self._warned = True
            self._warn(f"Recording will stop in {int(round(left))} seconds. Open the camera to keep it going.")

    def _finish(self, recording: Recording, writer: FragmentedMp4Writer, stopped_because: str) -> None:
        try:
            writer.close()
        except Exception:
            logger.exception("recording: could not finish %s", recording.name)
            stopped_because = ERROR
        with self._route_lock:
            self._to_queue = False  # the ring (if held) is fed again from the next keyframe
        with self._lock:
            self._listen()
        try:
            self.source.stop()
        except Exception:
            logger.debug("stopping the camera raised", exc_info=True)
        self._drain_queue()
        path = Path(recording.path)
        if not writer.started:  # not one keyframe arrived: there is no file to keep
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
            self._done()
            logger.warning("recording: %s ended before any video arrived (%s)", recording.name, stopped_because)
            self._warn("Recording failed: the camera sent no video")
            self._changed()
            self._reopen_for_settings()
            return
        recording.complete = True
        recording.stopped_because = stopped_because
        recording.ended_at = _iso(self._wall_clock())
        recording.duration_seconds = round(writer.duration_seconds, 3)
        recording.frames = writer.frames
        recording.bytes = _size(path)
        try:
            recording.write_sidecar(self._fsync)
        except OSError:
            logger.exception("recording: could not write the sidecar for %s", recording.name)
        self.last = recording
        self._done()  # only now "not recording": whoever sees that can already read `last`
        logger.info("recording: saved %s (%s)", _describe(recording), stopped_because)
        if stopped_because == STORAGE_FULL:
            self._warn(f"Recording stopped: the card is nearly full. Saved {_describe(recording)}")
        elif stopped_because == CAMERA_STOPPED:
            self._warn(f"Recording stopped: the camera stopped sending video. Saved {_describe(recording)}")
        elif stopped_because == ERROR:
            self._warn(f"Recording stopped: the file could not be written. Saved {_describe(recording)}")
        else:
            self._info(f"Recording saved: {_describe(recording)}")
        for listener in list(self._saved_listeners):
            try:
                listener(recording)
            except Exception:
                logger.exception("an on_saved handler raised")
        self._changed()
        self._reopen_for_settings()  # a camera setting changed during the recording: in force for the next one

    def _done(self) -> None:
        with self._lock:
            self._current = None
            self._thread = None
            for name, value in self._pending.items():  # changed during the recording: in force from the next one
                self._assign_limit(name, value)
            self._pending.clear()

    # -- telling people --------------------------------------------------------------------------------------------------

    def _changed(self) -> None:
        for listener in list(self._change_listeners):
            try:
                listener(self)
            except Exception:
                logger.debug("an on_change listener raised", exc_info=True)

    def _info(self, message: str) -> None:
        self._send("info", message)

    def _warn(self, message: str) -> None:
        self._send("warning", message)

    def _send(self, severity: str, message: str) -> None:
        if self.notify is None:
            return
        try:
            getattr(self.notify, severity)(message)
        except Exception:  # not connected right now: the log still has it
            logger.debug("could not send a recording notification", exc_info=True)


__all__ = [
    "FragmentedMp4Writer",
    "Recorder",
    "Recording",
    "RecordingStore",
    "clock_synchronised",
    "nal_units",
    "parameter_sets",
]
