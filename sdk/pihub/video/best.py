"""`best_camera_source()`: the right video source for whatever this machine has, so a program does not have to know.

    camera.use(best_camera_source())

The ladder, best first:

1. **The Pi's hardware H.264 encoder** (a Pi camera on a Pi that has the encoder: the Pi Zero to the Pi 4). Cheap (about 5% of a Pi 3A+ at 640x480); streams, snapshots and records.
2. **A Pi camera encoded on the CPU by libx264** (`SoftwareEncodedCameraSource`: a Pi with no hardware encoder, such as the Pi 5, or when `software_encoder=True`). The CPU does
   the encoding (about 57% of a Pi 3A+ at 640x480); it streams, takes snapshots AND records, because it hands on H.264 packets like the hardware source.
3. **A Pi camera whose raw frames `aiortc` encodes** (`PiCameraSource`), only if libx264 is not available to PyAV: streams and snapshots, but cannot be recorded.
4. **A moving test pattern**, only on a computer that is not a Raspberry Pi and has no camera: for trying a program anywhere.

A Raspberry Pi that should have a camera but cannot use one (no camera library, or none attached) is an ERROR, not a test pattern: showing a test pattern as if it were the camera
would hide the real problem. `require_encoded=True` (the Motion Trap) makes step 3 an error too, since it can never record.
"""

from __future__ import annotations

from typing import Optional, Tuple

from pihub.video.hardware import HardwareCameraSource
from pihub.video.software import SoftwareEncodedCameraSource
from pihub.video.sources import CameraUnavailable, EncodedTestPatternSource, PiCameraSource, VideoSource, running_on_a_pi

NEEDS_AN_ENCODER = (
    "recording needs an H.264 encoder, and this Raspberry Pi has neither the hardware one (any Pi before the Pi 5) nor libx264 for the CPU to use. "
    "Install PyAV with libx264: `sudo apt install python3-av` (a virtual environment needs `--system-site-packages`)."
)

NO_CAMERA_ON_A_PI = (
    "this Raspberry Pi has no usable camera. Check the camera's ribbon cable and that `rpicam-hello` (or `libcamera-hello`) shows a picture, and that the camera library is "
    "installed: `sudo apt install python3-picamera2` (a virtual environment needs `--system-site-packages`)."
)


def best_camera_source(
    size: Optional[Tuple[int, int]] = None,
    fps: Optional[int] = None,
    bitrate: Optional[int] = None,
    tuning: Optional[str] = None,
    *,
    software_encoder: bool = False,
    test_pattern: bool = False,
    require_encoded: bool = False,
    motion_frames: bool = False,
    orientation: Optional[str] = None,
) -> VideoSource:
    """The best source this machine can offer (see the module's ladder). `source.description` says which one it is, in a few words, for a log line.

    `size`, `fps`, `bitrate` and `tuning` are the source's own options (a value given is explicit and wins over one saved from the dashboard; `bitrate` is for the hardware
    encoder only). `software_encoder=True` skips the hardware encoder (for a Pi that has one but where the CPU is wanted, or to try the Pi 5's path). `test_pattern=True`
    forces the test pattern (an ENCODED one, as the hardware's is, so it can be recorded too). `require_encoded=True` is for a program that must record (the Motion Trap): it
    raises `CameraUnavailable` rather than return a source that cannot. `orientation` is "Normal", "Rotate 180°", "Mirror" or "Flip" (default: the saved Orientation setting, else Normal): the camera's image processor turns the picture, for a camera mounted the wrong way up.
    `motion_frames=True` makes a Pi camera produce a small picture beside the video for
    `source.grab_motion_frame()` (movement detection); the test pattern always offers one, and a source that cannot returns None from it. Raises `CameraUnavailable` on a Raspberry Pi that cannot use its camera.
    """
    if test_pattern:
        return EncodedTestPatternSource(size=size, fps=fps)
    if not software_encoder and HardwareCameraSource.available():
        return HardwareCameraSource(size=size, fps=fps, bitrate=bitrate, tuning=tuning, motion_frames=motion_frames, orientation=orientation)
    if SoftwareEncodedCameraSource.available():
        return SoftwareEncodedCameraSource(size=size, fps=fps, bitrate=bitrate, tuning=tuning, motion_frames=motion_frames, orientation=orientation)
    if PiCameraSource.available():
        if require_encoded:
            raise CameraUnavailable(NEEDS_AN_ENCODER)
        return PiCameraSource(size=size, fps=fps, tuning=tuning, orientation=orientation)
    if running_on_a_pi():
        raise CameraUnavailable(NO_CAMERA_ON_A_PI)
    return EncodedTestPatternSource(size=size, fps=fps)
