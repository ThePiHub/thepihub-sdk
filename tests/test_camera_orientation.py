"""The camera's Orientation setting: Normal, Rotate 180°, Mirror and Flip, turned by the camera's image processor (a libcamera `Transform`), for a camera mounted the wrong way up.

No Pi here: a fake picamera2 records the configuration it is given, and `make_transform` is faked so the tests say exactly which orientation reached the camera. What only
a real Pi can show: 90 and 270 degrees are NOT offered because the Pi's image processor cannot do them. Measured on a Pi Zero W (2026-10-06): every request that included a
transpose came back as a flip or a 180 degree rotation, with the picture still 640x480 (`test_no_orientation_asks_for_a_transpose` keeps that from being "added" later).
The picture itself being turned the right way was not compared pixel by pixel.
"""

from __future__ import annotations

import sys
import types
from typing import Any, List

import pytest

from pihub.video import HardwareCameraSource, PiCameraSource, SoftwareEncodedCameraSource, TestPatternSource, best_camera_source
from pihub.video import best as best_module
from pihub.video import hardware, sources
from pihub.video.sources import DEFAULT_ORIENTATION, ORIENTATIONS, CameraUnavailable, make_transform
from tests.test_camera_settings import INFO, change, config_home, fake_libcamera, saved, started  # noqa: F401

SIZE, FPS = (320, 240), 15
TURNED = [label for label in ORIENTATIONS if label != DEFAULT_ORIENTATION]


class FakeOutput:
    def __init__(self):
        self.recording = False


class FakeEncoder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakePicamera2:
    made: List["FakePicamera2"] = []

    def __init__(self, *args, **kwargs):
        self.configuration: Any = None
        FakePicamera2.made.append(self)

    def create_video_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        self.configuration = config

    def start(self):
        pass

    def start_recording(self, encoder, output):
        pass

    def stop(self):
        pass

    def stop_recording(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_picamera2(monkeypatch):
    FakePicamera2.made.clear()
    library = types.SimpleNamespace(Picamera2=FakePicamera2, H264Encoder=FakeEncoder, LibavH264Encoder=FakeEncoder, Output=FakeOutput)
    monkeypatch.setattr(sources, "import_picamera2", lambda: library)
    monkeypatch.setattr(hardware, "import_picamera2", lambda: library)


@pytest.fixture
def transforms(monkeypatch):
    """Replace libcamera's Transform with a note of which orientation was asked for."""
    asked: List[str] = []

    def fake(orientation: str) -> Any:
        asked.append(orientation)
        return ("transform", orientation)

    monkeypatch.setattr(sources, "make_transform", fake)
    return asked


@pytest.fixture
def opened():
    made: List[Any] = []
    yield made
    for source in made:
        while source.running:
            source.stop()


KINDS = [HardwareCameraSource, SoftwareEncodedCameraSource, PiCameraSource]


def make(kind, opened, **kwargs):
    source = kind(size=SIZE, fps=FPS, tuning="", **kwargs)  # tuning "": the camera's own, so no saved setting is read
    opened.append(source)
    return source


# -- the table ------------------------------------------------------------------------------------------------------------------


def test_the_four_orientations_and_none_of_them_is_a_quarter_turn():
    assert list(ORIENTATIONS) == ["Normal", "Rotate 180°", "Mirror", "Flip"]
    assert ORIENTATIONS["Normal"] == (False, False)
    assert ORIENTATIONS["Rotate 180°"] == (True, True)  # a half turn is both flips
    assert ORIENTATIONS["Mirror"] == (True, False)
    assert ORIENTATIONS["Flip"] == (False, True)


def test_no_orientation_asks_for_a_transpose(monkeypatch):
    """The Pi's image processor drops a transpose and quietly gives a flip or a half turn instead, so a quarter turn must never be offered."""
    seen = {}

    class Transform:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setitem(sys.modules, "libcamera", types.SimpleNamespace(Transform=Transform))
    for label in ORIENTATIONS:
        seen.clear()
        make_transform(label)
        assert set(seen) == {"hflip", "vflip"}  # never transpose


def test_make_transform_gives_libcameras_flags(monkeypatch):
    seen = {}

    class Transform:
        def __init__(self, hflip=0, vflip=0):
            seen.update(hflip=hflip, vflip=vflip)

    monkeypatch.setitem(sys.modules, "libcamera", types.SimpleNamespace(Transform=Transform))
    make_transform("Rotate 180°")

    assert seen == {"hflip": 1, "vflip": 1}


def test_make_transform_says_so_when_libcamera_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "libcamera", None)  # makes `from libcamera import Transform` raise ImportError

    with pytest.raises(CameraUnavailable, match="libcamera"):
        make_transform("Mirror")


# -- what reaches the camera -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_normal_leaves_the_camera_configuration_alone_and_never_touches_libcamera(kind, opened, transforms):
    source = make(kind, opened)
    source.start()

    assert "transform" not in FakePicamera2.made[0].configuration
    assert transforms == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("label", TURNED)
def test_a_turned_orientation_reaches_the_camera_as_a_transform(kind, label, opened, transforms):
    source = make(kind, opened, orientation=label)
    source.start()

    configuration = FakePicamera2.made[0].configuration
    assert configuration["transform"] == ("transform", label)
    assert configuration["main"]["size"] == SIZE  # nothing else about the picture changes
    assert configuration["controls"] == {"FrameRate": FPS}


def test_the_orientation_goes_with_the_small_motion_picture_too(opened, transforms):
    source = make(HardwareCameraSource, opened, orientation="Rotate 180°", motion_frames=True)
    source.start()

    configuration = FakePicamera2.made[0].configuration
    assert "lores" in configuration and configuration["transform"] == ("transform", "Rotate 180°")


def test_an_orientation_that_is_not_one_of_the_four_is_refused():
    with pytest.raises(ValueError, match="orientation must be one of"):
        HardwareCameraSource(orientation="Rotate 90°")
    with pytest.raises(ValueError, match="orientation must be one of"):
        PiCameraSource(orientation="Transpose")


def test_best_camera_source_passes_it_on(monkeypatch):
    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: True))
    monkeypatch.setattr(best_module, "running_on_a_pi", lambda: True)

    assert best_camera_source(tuning="", orientation="Flip").orientation == "Flip"
    assert best_camera_source(tuning="").orientation == "Normal"

    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: True))
    assert best_camera_source(tuning="", orientation="Mirror").orientation == "Mirror"

    monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(PiCameraSource, "available", staticmethod(lambda: True))
    assert best_camera_source(tuning="", orientation="Rotate 180°").orientation == "Rotate 180°"


# -- the dashboard setting ---------------------------------------------------------------------------------------------------------


def test_a_real_camera_offers_orientation_in_the_camera_group_and_a_test_pattern_does_not():
    _, real = started(HardwareCameraSource(tuning=""))
    widget = real.widgets()["camera-orientation"]
    assert widget["type"] == "Select"
    options = next(p for p in widget["propertySchema"] if p["name"] == "value")["options"]
    assert options == ["Normal", "Rotate 180°", "Mirror", "Flip"]
    assert widget["defaultProperties"]["value"] == "Normal"
    assert widget["defaultProperties"]["group"] == "Camera"

    _, pattern = started(TestPatternSource())
    assert "camera-orientation" not in pattern.widgets()


def test_a_change_while_the_camera_is_closed_is_in_effect_at_once_and_is_saved(config_home):
    source = HardwareCameraSource(tuning="")
    device, transport = started(source)

    change(device, "camera-orientation", "Rotate 180°")

    assert source.orientation == "Rotate 180°"
    assert saved(config_home)[f"{INFO}.camera-orientation"] == "Rotate 180°"
    assert transport.runtime("camera-orientation")[-1] == "Rotate 180°"


def test_a_change_while_the_camera_is_open_waits_for_the_next_start_and_reaches_the_camera(opened, transforms):
    source = make(HardwareCameraSource, opened)
    device, transport = started(source)
    source.start()

    change(device, "camera-orientation", "Flip")

    assert source.orientation == "Normal"  # the open camera keeps what it opened with
    assert "transform" not in FakePicamera2.made[0].configuration
    assert any("Stop and start the camera" in message for message in transport.notifications())

    source.stop()
    source.start()
    assert FakePicamera2.made[-1].configuration["transform"] == ("transform", "Flip")


def test_a_value_that_is_not_an_option_changes_nothing(config_home):
    source = HardwareCameraSource(tuning="")
    device, transport = started(source)

    change(device, "camera-orientation", "Rotate 90°")

    assert source.orientation == "Normal"
    assert saved(config_home) == {}
    assert any("Setting not changed" in message for message in transport.notifications())


def test_a_saved_orientation_is_used_by_the_next_run(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text('{"device-information.camera-orientation": "Mirror"}')
    source = HardwareCameraSource(tuning="")
    started(source)

    assert source.orientation == "Mirror"


def test_an_orientation_given_in_code_beats_a_saved_one_and_a_dashboard_change_to_it_is_not_saved(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text('{"device-information.camera-orientation": "Mirror"}')
    source = HardwareCameraSource(tuning="", orientation="Flip")
    device, _ = started(source)

    assert source.orientation == "Flip"
    change(device, "camera-orientation", "Rotate 180°")
    assert source.orientation == "Rotate 180°"
    assert saved(config_home).get(f"{INFO}.camera-orientation") == "Mirror"  # unchanged: a value passed in code is not saved over
