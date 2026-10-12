"""The Pi's hardware H.264 encoder, as a video source.

The Raspberry Pi has a video encoder on its GPU. `picamera2`'s `H264Encoder` drives it (through V4L2), so the
CPU never encodes anything: on a Pi 3A+ that is about 4% of the board at 640x480, against about 57% for aiortc's
software H.264 with a lit scene. This is what lets a Pi Zero W stream at
roughly 10% CPU (an earlier version did it through the old MMAL stack with a patched aiortc; current aiortc sends
pre-encoded packets natively, so no patch is needed).

`HardwareCameraSource` yields those already-encoded H.264 packets (`encoded = True`); `aiortc` packetises and
encrypts them and does not encode again. Three things a software source never has to think about:

* **Keyframes.** A viewer that joins, or loses its picture, needs a fresh keyframe. The source repeats the stream
  headers with every keyframe and makes one regularly (`keyframe_seconds`), and `request_keyframe()` forces one on
  demand (a browser asking for it, a viewer starting).
* **Back-pressure.** Encoded packets cannot be thinned like raw frames: dropping one breaks the picture until the next
  keyframe. If the buffer overflows it is emptied, and everything is skipped until the next keyframe arrives.
* **Stills.** The encoder's output is not a picture, so a snapshot captures from the same running camera instead.
"""

from __future__ import annotations

import logging
import os
import queue
import time
from typing import Any, Callable, Optional, Tuple

from pihub.video.sources import (
    DEFAULT_FPS,
    DEFAULT_SIZE,
    MOTION_FRAME_SIZE,
    SETTLE_FRAMES,
    CameraUnavailable,
    VideoSource,
    close_quietly,
    import_picamera2,
    new_camera,
)

logger = logging.getLogger(__name__)

# The hardware encoder's device on the Pi models that have one (not a Pi 5, which has no H.264 encoder).
ENCODER_DEVICE = "/dev/video11"
DEFAULT_BITRATE = 1_000_000
# What the Bitrate setting offers; the top is provisional, still to be measured. The bottom is a floor for a picture you can still use: at 100,000 bit/s a 640x480 picture at 15 frames
# a second is so smeared that small print on a keyboard is unreadable (seen on a Pi Zero W, 2026-10-07: a saved 100,000 looked like a fault in the camera for an afternoon).
BITRATE_RANGE = (250_000, 4_000_000)
BITRATE_STEP = 50_000
# Constrained Baseline: what a browser is certain to decode, and the profile the hardware encoder is proven with.
DEFAULT_PROFILE = "constrained baseline"
READ_TIMEOUT_SECONDS = 2.0
# The camera needs about a dozen frames to start producing video (its exposure settles), so at a low frame rate the first packet is seconds away:
# measured on a Zero W, 0.5 s at 15 fps, 1.9 s at 5 fps, about 8 s at 2 fps. A fixed wait gave up on a slow start and the picture stayed black.
STARTUP_FRAMES = 12


def read_timeout_seconds(fps: int) -> float:
    """How long to wait for the next packet before deciding the encoder produced none: at least 2 s, and long enough for a dozen frames at this rate."""
    return max(READ_TIMEOUT_SECONDS, STARTUP_FRAMES / max(1, fps))


# A stream that has not asked for a packet for this long has stopped reading (the camera is open for a recording, say):
# the buffer then discards instead of overflowing, so it never asks the encoder for keyframes nobody will see.
READER_IDLE_SECONDS = 1.0


def crop_yuv420(array: Any, width: int, height: int) -> Any:
    """The camera's YUV420 picture without the padding it adds to every row.

    The Pi's image processor pads each row to a multiple of 64 bytes, so `capture_array` hands back `stride` columns, not `width`: an 800x600 picture arrives
    832 wide, the last 32 columns zero. Read as a picture, all-zero is solid green (seen on a Pi Zero, 2026-10-08: a 31-pixel green strip down the right edge of every
    snapshot at 800x600). Of the sizes the dashboard offers only 800x600 has padding; the others are already a multiple of 64.

    The array is planar and packed: `height` rows of luma, then the U plane, then the V plane, each chroma plane being `height / 2` lines of `stride / 2` bytes laid
    end to end (so `height / 4` rows of `stride`). Cropping is therefore not just `[:, :width]`: each plane is cropped on its own and packed again at `width`.
    A picture with no padding is returned as it is, and so is a layout this does not recognise (a height that is not a multiple of 4, say), which is how stills
    have always been saved."""
    rows, stride = array.shape
    if stride == width:
        return array
    if stride < width or width % 2 or height % 4 or rows != height * 3 // 2:
        logger.warning("a still of an unexpected shape %s for %dx%d is saved as the camera gave it", tuple(array.shape), width, height)
        return array
    import numpy as np

    quarter = height // 4
    luma = array[:height, :width]
    u = array[height : height + quarter].reshape(height // 2, stride // 2)[:, : width // 2].reshape(quarter, width)
    v = array[height + quarter :].reshape(height // 2, stride // 2)[:, : width // 2].reshape(quarter, width)
    return np.concatenate([luma, u, v])


class PacketBuffer:
    """A small thread-safe buffer between the encoder's thread and the video track.

    Pure Python, so the overflow rule can be tested without a camera. Holds about two seconds of packets. On overflow
    it empties itself and then discards everything until the next keyframe: a decoder cannot resume in the middle of
    a group of pictures, and a picture that is a second late is better than a smeared one.

    **Nobody reading is not an overflow.** The camera can be open with no stream at all (a recorder, which gets its
    packets separately). Once no reader has asked for a packet for `READER_IDLE_SECONDS`, the buffer empties itself and
    quietly discards; when a reader comes back it starts from the next keyframe, and asks for one at once.
    """

    def __init__(self, maxsize: int, on_overflow: Optional[Any] = None, clock: Any = time.monotonic) -> None:
        self._queue: "queue.Queue[Optional[Tuple[bytes, bool]]]" = queue.Queue(maxsize)
        self._waiting_for_keyframe = False
        self._on_overflow = on_overflow
        self._clock = clock
        self._readers = 0  # readers blocked in get() right now
        self._last_read = clock()  # a new buffer counts as read: its stream is about to start
        self._idle = False
        self.dropped = 0
        self.discarded_idle = 0

    def _reader_idle(self) -> bool:
        return self._readers == 0 and self._clock() - self._last_read > READER_IDLE_SECONDS

    def put(self, data: bytes, keyframe: bool) -> None:
        if self._reader_idle():
            if not self._idle:
                self._idle = True
                self.discarded_idle += self._drain()
            self._waiting_for_keyframe = True
            self.discarded_idle += 1
            return
        if self._idle:  # a reader is back: it needs a keyframe, and should not wait up to a second for the regular one
            self._idle = False
            if not keyframe and self._on_overflow is not None:
                self._on_overflow()
        if self._waiting_for_keyframe and not keyframe:
            self.dropped += 1
            return
        try:
            self._queue.put_nowait((data, keyframe))
            self._waiting_for_keyframe = False
        except queue.Full:
            self.dropped += self._drain() + 1
            self._waiting_for_keyframe = True
            if self._on_overflow is not None:
                self._on_overflow()  # ask the encoder for a keyframe now, so the wait is short

    def get(self, timeout: float) -> Tuple[bytes, bool]:
        """The next packet, blocking. Raises `queue.Empty` on timeout and `CameraUnavailable` once closed."""
        self._readers += 1
        try:
            item = self._queue.get(timeout=timeout)
        finally:
            self._readers -= 1
            self._last_read = self._clock()
        if item is None:
            raise CameraUnavailable("the camera stopped")
        return item

    def close(self) -> None:
        """Wake a blocked reader at once, so stopping the camera never waits out a read."""
        self._drain()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    def _drain(self) -> int:
        count = 0
        while True:
            try:
                self._queue.get_nowait()
                count += 1
            except queue.Empty:
                return count


class HardwareCameraSource(VideoSource):
    """A Pi camera whose video is encoded to H.264 by the Pi's GPU, and handed on packet by packet."""

    encoded = True
    real_camera = True
    description = "the Pi's hardware H.264 encoder"
    encoder_name = "hardware encoder"  # what an error message calls the encoder

    def __init__(
        self,
        size: Optional[Tuple[int, int]] = None,
        fps: Optional[int] = None,
        bitrate: Optional[int] = None,
        keyframe_seconds: float = 1.0,
        profile: str = DEFAULT_PROFILE,
        tuning: Optional[str] = None,
        on_open: Optional[Callable[[Any], None]] = None,
        motion_frames: bool = False,
        orientation: Optional[str] = None,
    ) -> None:
        super().__init__(size, fps)
        self._set_orientation(orientation)  # "Normal", "Rotate 180°", "Mirror" or "Flip": the picture is turned by the camera's image processor (free)
        self.tuning = tuning  # None: the saved setting (`pihub camera setup`); or a libcamera tuning, e.g. "imx219_noir" (see `new_camera`)
        # `motion_frames=True` makes the camera produce a second, small picture beside the video (the camera's ISP scales it, not the CPU), which
        # `grab_motion_frame()` returns: for detecting movement by comparing pictures. Off by default: a program that does not use it pays nothing.
        self.motion_frames = motion_frames
        # `on_open(camera)`: called with the `Picamera2` each time the camera opens, after it is configured and before it starts recording
        # (see `VideoSource.camera`). Do not call `start()` or `stop()` on this source from it. If it raises, the camera is closed and
        # `start()` raises `CameraUnavailable`.
        self.on_open = on_open
        if keyframe_seconds <= 0:
            raise ValueError("keyframe_seconds must be positive")
        if bitrate is not None:
            self.explicit.add("bitrate")
        self.bitrate = bitrate if bitrate is not None else DEFAULT_BITRATE
        self.keyframe_seconds = keyframe_seconds
        self.profile = profile
        self._camera: Optional[Any] = None
        self._encoder: Optional[Any] = None
        self._buffer: Optional[PacketBuffer] = None
        self._opened_at = 0.0

    def _assign(self, name: str, value: Any) -> None:
        if name == "bitrate":
            self.bitrate = int(value)
        else:
            super()._assign(name, value)

    def add_settings(self, section: Any, *, sensor: Optional[str] = None) -> None:
        super().add_settings(section, sensor=sensor)
        self._add_setting(
            section, "camera-bitrate", "bitrate", "Slider", "Bitrate", self.bitrate, units="bit/s",
            minimum=min(BITRATE_RANGE[0], self.bitrate), maximum=max(BITRATE_RANGE[1], self.bitrate), step=BITRATE_STEP,
        )

    @staticmethod
    def available() -> bool:
        """Whether this machine has a hardware H.264 encoder and the library to drive it."""
        if not os.path.exists(ENCODER_DEVICE):
            return False
        try:
            import_picamera2()
        except CameraUnavailable:
            return False
        return True

    def _open(self) -> None:
        if self._camera is not None:
            return
        picamera2 = import_picamera2()
        buffer = PacketBuffer(maxsize=max(8, int(self.fps * 2)), on_overflow=self.request_keyframe)
        output = self._make_output(picamera2.Output, buffer)
        camera = None
        try:
            camera = new_camera(picamera2.Picamera2, self.tuning)
            streams: dict = {"main": {"size": (self.width, self.height), "format": "YUV420"}}
            if self.motion_frames:
                streams["lores"] = {"size": MOTION_FRAME_SIZE, "format": "YUV420"}
            camera.configure(camera.create_video_configuration(controls={"FrameRate": self.fps}, **streams, **self.camera_transform()))
            if self.on_open is not None:
                self.on_open(camera)
            encoder = self._make_encoder(picamera2)
            camera.start_recording(encoder, output)
        except Exception as error:  # no camera attached, another program holds it, no encoder, or `on_open` raised
            close_quietly(camera)
            buffer.close()
            raise CameraUnavailable(f"could not start the camera's {self.encoder_name}: {error}") from error
        self._camera, self._encoder, self._buffer = camera, encoder, buffer
        self._opened_at = time.monotonic()

    def _make_encoder(self, picamera2: Any) -> Any:
        """The encoder that turns the camera's pictures into H.264 packets: here the Pi's hardware one. A subclass can choose another (see `software.py`)."""
        return picamera2.H264Encoder(
            bitrate=self.bitrate,
            repeat=True,  # repeat the stream headers with every keyframe: a viewer can join at any of them
            iperiod=max(1, int(round(self.fps * self.keyframe_seconds))),
            framerate=self.fps,
            profile=self.profile,
        )

    def _make_output(self, output_base: Any, buffer: PacketBuffer) -> Any:
        source = self

        class PacketOutput(output_base):  # type: ignore[misc, valid-type]
            """Receives each encoded picture from the encoder's thread: into the stream's buffer, and to any packet listener (a recorder)."""

            def outputframe(self, frame, keyframe=True, timestamp=None, packet=None, audio=False):  # noqa: D401
                if self.recording:
                    data = bytes(frame)  # a copy: the encoder reuses its buffer
                    buffer.put(data, bool(keyframe))
                    source._emit_packet(data, bool(keyframe), timestamp)

        return PacketOutput()

    def _close(self) -> None:
        camera, self._camera, self._encoder = self._camera, None, None
        buffer, self._buffer = self._buffer, None
        if buffer is not None:
            buffer.close()
        if camera is not None:
            try:
                camera.stop_recording()
                camera.close()
            except Exception:  # a camera that is already gone
                logger.debug("closing the camera raised", exc_info=True)

    def _read(self) -> Any:  # -> av.Packet
        import av

        buffer = self._buffer
        if buffer is None:
            raise CameraUnavailable("the camera is not running")
        try:
            data, _keyframe = buffer.get(read_timeout_seconds(self.fps))
        except queue.Empty:
            raise CameraUnavailable("the hardware encoder produced no video") from None
        return av.Packet(data)  # Annex B H.264; the track stamps its time

    def request_keyframe(self) -> None:
        """Make the encoder produce a keyframe now. Best effort: on any failure the regular keyframes still come."""
        vd = getattr(self._encoder, "vd", None)
        if vd is None:
            return
        try:
            import fcntl

            from videodev2 import V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME, VIDIOC_S_CTRL, v4l2_control

            control = v4l2_control()
            control.id = V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME
            control.value = 1
            fcntl.ioctl(vd, VIDIOC_S_CTRL, control)
        except Exception:
            logger.debug("could not force a keyframe", exc_info=True)

    def grab_motion_frame(self) -> Optional[Any]:
        """The camera's small greyscale picture (`MOTION_FRAME_SIZE`, a `uint8` array of rows by columns), or None if this source was not made with
        `motion_frames=True` or the camera is not running. Measured on a Pi Zero W (2026-10-06): about 15 ms to grab and 4 comparisons a second cost
        about 3% of the CPU, and the video kept its full frame rate (grabbing the full-size picture instead cost 27% and dropped it to 12.7 frames a second)."""
        camera = self._camera
        if camera is None or not self.motion_frames:
            return None
        width, height = MOTION_FRAME_SIZE
        try:
            picture = camera.capture_array("lores")
        except Exception:  # the camera was stopped while waiting for a picture
            logger.debug("could not capture a motion picture", exc_info=True)
            return None
        return picture[:height, :width]  # a YUV420 array: the first `height` rows are the brightness, the rest colour

    def grab_still(self) -> Any:  # -> av.VideoFrame
        """A picture from the running camera. The encoder's output is not a picture, so capture beside it.

        `picamera2` captures from a stream that is also feeding the encoder, so a snapshot and live video coexist.
        """
        import av

        camera = self._camera
        if camera is None:
            raise CameraUnavailable("the camera is not running")
        # Just started: read a few frames first so auto-exposure has settled. Already running: one is enough.
        count = SETTLE_FRAMES if time.monotonic() - self._opened_at < 2.0 else 1
        array = None
        for _ in range(count):
            array = camera.capture_array("main")
        return av.VideoFrame.from_ndarray(crop_yuv420(array, self.width, self.height), format="yuv420p")
