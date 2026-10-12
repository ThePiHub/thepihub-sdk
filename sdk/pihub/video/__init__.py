"""Live video sources for a device.

A **source** produces camera frames on request. Nothing here starts a camera
by itself: the platform's media grant does, when someone presses "Start
camera", and the source is stopped again the moment the grant ends. Sources
are the seam where the encoder can change later: today a source yields raw
frames and `aiortc` encodes them (H.264, software); a source that yields
already-encoded frames from a hardware encoder is the intended next step for
small boards.

```python
from pihub import Device, Program
from pihub.video import PiCameraSource, best_camera_source

camera = program.output(id="camera", type="Video")
camera.use(best_camera_source())                        # whatever this machine has: hardware encoder, else the Pi camera in software (a Pi 5), else a test pattern
camera.use(PiCameraSource(size=(640, 480), fps=15))     # software H.264 (about 57% of a Pi 3A+)
camera.use(HardwareCameraSource(size=(640, 480)))       # the Pi's hardware encoder (about 4%): use this on a small Pi
camera.use(TestPatternSource())                         # no hardware at all

# Recording to the device's own storage, from the same encode as the stream (an encoded source only)
recorder = Recorder(HardwareCameraSource(), directory="~/thepihub-recordings", notify=program)
recorder.start(reason="motion"); recorder.stop()        # `reason` is a label for the sidecar and the log

# Pre-roll: keep the seconds before a trigger. arm() keeps the camera encoding while idle (it costs what recording does)
recorder = Recorder(source, pre_roll_seconds=5); recorder.arm()

# Optional: tell the user, once, if a NoIR camera's picture is pink because `pihub camera setup` was never run
watch_camera_colour(camera_source, device.information.warning)
```

Reaching the camera. The package does not wrap every camera feature; `picamera2` is yours to use. A Pi camera source hands over its
`Picamera2` as `source.camera` while it is running (None otherwise), and `on_open` runs each time the camera opens, after it is
configured and before it starts. The camera closes when its last user stops, so set anything that must hold on every open in
`on_open`, not once afterwards:

```python
def tune(camera):                                        # called on every open, before the camera starts
    camera.set_controls({"ExposureTime": 20000, "AnalogueGain": 1.0})

source = HardwareCameraSource(size=(1280, 720), on_open=tune)
...
source.camera.capture_metadata()                         # the live Picamera2, while the source is running
```

The source still owns the camera: do not stop or close it, change the stream's size or format, or call `start()`/`stop()` on the source
from `on_open`. Switching to another mode (a full-resolution still) interrupts the stream while it happens. If `on_open` raises, the camera
is closed and `start()` raises `CameraUnavailable`.

Importing this package imports neither `aiortc` nor `picamera2`.
"""

from pihub.video.best import best_camera_source
from pihub.video.colour import watch_camera_colour
from pihub.video.hardware import HardwareCameraSource
from pihub.video.recording import Recorder, Recording, RecordingStore
from pihub.video.software import SoftwareEncodedCameraSource
from pihub.video.sources import (
    CameraUnavailable,
    EncodedTestPatternSource,
    PiCameraSource,
    TestPatternSource,
    VideoSource,
)
from pihub.video.stills import capture_still

__all__ = [
    "CameraUnavailable",
    "EncodedTestPatternSource",
    "HardwareCameraSource",
    "PiCameraSource",
    "Recorder",
    "Recording",
    "RecordingStore",
    "SoftwareEncodedCameraSource",
    "TestPatternSource",
    "VideoSource",
    "best_camera_source",
    "capture_still",
    "watch_camera_colour",
]
