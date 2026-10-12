"""`VideoSource.camera` and `on_open`: how a tinkerer reaches the `Picamera2` behind a Pi camera source.

No Pi here: a fake picamera2 records every call in order, so the tests can say WHEN `on_open` runs (after `configure`, before the camera starts)
and what a failed open leaves behind. What only a real Pi can show (a control taking effect, the stream still playing) has to be checked on a real Pi.
"""

from __future__ import annotations

import types
from typing import Any, List

import pytest

from pihub.video import HardwareCameraSource, PiCameraSource, SoftwareEncodedCameraSource, TestPatternSource
from pihub.video import hardware, sources
from pihub.video.sources import CameraUnavailable

SIZE, FPS = (320, 240), 15


class FakeOutput:
    def __init__(self):
        self.recording = False


class FakeEncoder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakePicamera2:
    """Records the order of calls; `fail_on` names a call that raises."""

    made: List["FakePicamera2"] = []
    fail_on: str = ""

    def __init__(self, *args, **kwargs):
        self.calls: List[str] = []
        self.closed = False
        FakePicamera2.made.append(self)

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if FakePicamera2.fail_on == name:
            raise RuntimeError(f"{name} failed")

    def create_video_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        self._record("configure")

    def start(self):
        self._record("start")

    def start_recording(self, encoder, output):
        self._record("start_recording")

    def stop(self):
        self._record("stop")

    def stop_recording(self):
        self._record("stop_recording")

    def close(self):
        self.closed = True
        self.calls.append("close")


@pytest.fixture(autouse=True)
def fake_picamera2(monkeypatch):
    FakePicamera2.made.clear()
    FakePicamera2.fail_on = ""
    library = types.SimpleNamespace(Picamera2=FakePicamera2, H264Encoder=FakeEncoder, LibavH264Encoder=FakeEncoder, Output=FakeOutput)
    monkeypatch.setattr(sources, "import_picamera2", lambda: library)
    monkeypatch.setattr(hardware, "import_picamera2", lambda: library)
    return library


# The three ways a Pi camera source is made; each takes `on_open`, and each has its own start call.
KINDS = [
    pytest.param(PiCameraSource, "start", id="raw"),
    pytest.param(HardwareCameraSource, "start_recording", id="hardware"),
    pytest.param(SoftwareEncodedCameraSource, "start_recording", id="software"),
]


def make(kind, **kwargs):
    return kind(size=SIZE, fps=FPS, tuning="", **kwargs)  # tuning "": the camera's own, so no saved setting is read


@pytest.fixture
def opened():
    """Sources made in a test, stopped afterwards so no camera is left running."""
    made: List[Any] = []
    yield made
    for source in made:
        while source.running:
            source.stop()


# -- `camera` ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_camera_is_none_until_it_runs_then_the_picamera2_then_none_again(kind, start_call, opened):
    source = make(kind)
    opened.append(source)
    assert source.camera is None
    source.start()
    assert source.camera is FakePicamera2.made[0]
    source.stop()
    assert source.camera is None


def test_a_source_with_no_camera_has_none():
    source = TestPatternSource(size=SIZE, fps=FPS)
    source.start()
    try:
        assert source.camera is None
    finally:
        source.stop()


def test_a_second_user_shares_the_same_camera_object(opened):
    source = make(PiCameraSource)
    opened.append(source)
    source.start()
    first = source.camera
    source.start()
    assert source.camera is first
    source.stop()
    assert source.camera is first  # still one user left
    source.stop()
    assert source.camera is None


# -- `on_open` --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_on_open_gets_the_camera_after_configure_and_before_it_starts(kind, start_call, opened):
    seen = []

    def on_open(camera):
        seen.append((camera, list(camera.calls)))

    source = make(kind, on_open=on_open)
    opened.append(source)
    source.start()
    camera = FakePicamera2.made[0]
    assert [c for c, _ in seen] == [camera]
    assert seen[0][1] == ["configure"]  # configured, nothing started yet
    assert camera.calls[:2] == ["configure", start_call]


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_on_open_runs_once_per_open_not_once_per_user(kind, start_call, opened):
    count = []
    source = make(kind, on_open=lambda camera: count.append(camera))
    opened.append(source)
    source.start()
    source.start()  # a snapshot during a stream shares the open camera
    assert len(count) == 1
    source.stop()
    source.stop()  # the last user: the camera closes
    source.start()  # opens again, so it runs again, on a new camera
    assert len(count) == 2
    assert count[0] is not count[1]


def test_on_open_can_be_set_after_the_source_is_made(opened):
    source = make(PiCameraSource)
    opened.append(source)
    seen = []
    source.on_open = seen.append
    source.start()
    assert len(seen) == 1


def test_without_on_open_nothing_changes(opened):
    source = make(HardwareCameraSource)
    opened.append(source)
    source.start()
    assert FakePicamera2.made[0].calls == ["configure", "start_recording"]


# -- a failed open ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_a_callback_that_raises_fails_the_open_and_closes_the_camera(kind, start_call, opened):
    def on_open(camera):
        raise ValueError("bad control")

    source = make(kind, on_open=on_open)
    opened.append(source)
    with pytest.raises(CameraUnavailable, match="bad control"):
        source.start()
    camera = FakePicamera2.made[0]
    assert camera.closed  # not left holding the device
    assert start_call not in camera.calls  # it never started
    assert source.camera is None
    assert not source.running


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_a_start_that_fails_closes_the_camera_too(kind, start_call, opened):
    """The leak that existed before `on_open`: a camera whose `configure` or `start` raised was never closed."""
    FakePicamera2.fail_on = start_call
    source = make(kind)
    opened.append(source)
    with pytest.raises(CameraUnavailable):
        source.start()
    assert FakePicamera2.made[0].closed
    assert source.camera is None


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_a_configure_that_fails_closes_the_camera(kind, start_call, opened):
    FakePicamera2.fail_on = "configure"
    source = make(kind)
    opened.append(source)
    with pytest.raises(CameraUnavailable):
        source.start()
    assert FakePicamera2.made[0].closed


@pytest.mark.parametrize("kind, start_call", KINDS)
def test_a_source_can_be_started_again_after_a_failed_open(kind, start_call, opened):
    calls = []

    def on_open(camera):
        calls.append(camera)
        if len(calls) == 1:
            raise ValueError("first time only")

    source = make(kind, on_open=on_open)
    opened.append(source)
    with pytest.raises(CameraUnavailable):
        source.start()
    source.start()
    assert source.running
    assert source.camera is FakePicamera2.made[1]
