"""`grab_motion_frame()`: a small greyscale picture for detecting movement, from the camera's own second ("lores") stream beside the video.

No Pi here: a fake picamera2 records how the camera was configured and answers `capture_array("lores")`. What only a real Pi can show is the COST (measured on a Pi Zero W
on 2026-10-06: 4 grabs a second added about 3% CPU and the video kept its full 15 frames a second), so that is in the docstring, not asserted here.
"""

from __future__ import annotations

import types
from typing import Any, List

import numpy as np
import pytest

from pihub.video import HardwareCameraSource, PiCameraSource, SoftwareEncodedCameraSource, TestPatternSource, best_camera_source
from pihub.video import best as best_module
from pihub.video import hardware, sources
from pihub.video.sources import MOTION_FRAME_SIZE, VideoSource

SIZE, FPS = (320, 240), 15
WIDTH, HEIGHT = MOTION_FRAME_SIZE


class FakeOutput:
    def __init__(self):
        self.recording = False


class FakeEncoder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakePicamera2:
    made: List["FakePicamera2"] = []
    capture_fails = False

    def __init__(self, *args, **kwargs):
        self.configuration: Any = None
        self.captured: List[str] = []
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

    def capture_array(self, name):
        self.captured.append(name)
        if FakePicamera2.capture_fails:
            raise RuntimeError("the camera stopped")
        # A YUV420 picture: `height` rows of brightness (all 100), then `height / 2` rows of colour (all 7).
        picture = np.full((HEIGHT * 3 // 2, WIDTH), 7, dtype=np.uint8)
        picture[:HEIGHT] = 100
        return picture


@pytest.fixture(autouse=True)
def fake_picamera2(monkeypatch):
    FakePicamera2.made.clear()
    FakePicamera2.capture_fails = False
    library = types.SimpleNamespace(Picamera2=FakePicamera2, H264Encoder=FakeEncoder, LibavH264Encoder=FakeEncoder, Output=FakeOutput)
    monkeypatch.setattr(sources, "import_picamera2", lambda: library)
    monkeypatch.setattr(hardware, "import_picamera2", lambda: library)


@pytest.fixture
def opened():
    made: List[Any] = []
    yield made
    for source in made:
        while source.running:
            source.stop()


def make(kind, opened, **kwargs):
    source = kind(size=SIZE, fps=FPS, tuning="", **kwargs)  # tuning "": the camera's own, so no saved setting is read
    opened.append(source)
    return source


# -- a source that was not asked for these pays nothing and returns nothing ----------------------------------------------------


def test_a_source_that_offers_none_returns_none():
    assert VideoSource(size=SIZE, fps=FPS).grab_motion_frame() is None


@pytest.mark.parametrize("kind", [HardwareCameraSource, SoftwareEncodedCameraSource])
def test_the_camera_is_configured_with_no_second_stream_unless_asked(kind, opened):
    source = make(kind, opened)
    source.start()

    assert "lores" not in FakePicamera2.made[0].configuration
    assert source.grab_motion_frame() is None
    assert FakePicamera2.made[0].captured == []  # not even a capture was attempted


def test_the_raw_camera_source_offers_none(opened):
    source = make(PiCameraSource, opened)
    source.start()

    assert source.grab_motion_frame() is None


# -- asked for -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [HardwareCameraSource, SoftwareEncodedCameraSource])
def test_asked_for_the_camera_makes_a_small_second_stream_beside_the_video(kind, opened):
    source = make(kind, opened, motion_frames=True)
    source.start()

    configuration = FakePicamera2.made[0].configuration
    assert configuration["main"] == {"size": SIZE, "format": "YUV420"}  # the video is unchanged
    assert configuration["lores"] == {"size": MOTION_FRAME_SIZE, "format": "YUV420"}
    assert configuration["controls"] == {"FrameRate": FPS}


@pytest.mark.parametrize("kind", [HardwareCameraSource, SoftwareEncodedCameraSource])
def test_the_picture_is_only_the_brightness_rows(kind, opened):
    source = make(kind, opened, motion_frames=True)
    source.start()

    picture = source.grab_motion_frame()

    assert FakePicamera2.made[0].captured == ["lores"]
    assert picture.shape == (HEIGHT, WIDTH)
    assert picture.dtype == np.uint8
    assert (picture == 100).all()  # none of the colour rows (7) came along


def test_no_picture_before_the_camera_starts_or_after_it_stops(opened):
    source = make(HardwareCameraSource, opened, motion_frames=True)
    assert source.grab_motion_frame() is None

    source.start()
    assert source.grab_motion_frame() is not None
    source.stop()
    assert source.grab_motion_frame() is None


def test_a_camera_that_fails_to_give_a_picture_returns_none_not_an_error(opened):
    source = make(HardwareCameraSource, opened, motion_frames=True)
    source.start()
    FakePicamera2.capture_fails = True

    assert source.grab_motion_frame() is None


def test_best_camera_source_passes_the_option_on(monkeypatch):
    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: True))
    monkeypatch.setattr(best_module, "running_on_a_pi", lambda: True)

    assert best_camera_source(tuning="", motion_frames=True).motion_frames is True
    assert best_camera_source(tuning="").motion_frames is False

    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: True))
    assert best_camera_source(tuning="", motion_frames=True).motion_frames is True


# -- the test pattern: so a program can be tried with no camera ---------------------------------------------------------------


def test_the_test_pattern_offers_a_picture_that_moves():
    source = TestPatternSource(size=(640, 480), fps=15)
    source.start()
    try:
        first = source.grab_motion_frame()
        for _ in range(3):
            source.read()  # the pattern moves on a frame at a time
        second = source.grab_motion_frame()
    finally:
        source.stop()

    assert first.shape == second.shape == (HEIGHT, WIDTH)
    assert first.dtype == np.uint8
    assert (first != second).any()


def test_the_test_pattern_picture_is_steady_for_a_given_frame():
    source = TestPatternSource(size=(640, 480), fps=15)

    assert (source.grab_motion_frame() == source.grab_motion_frame()).all()
