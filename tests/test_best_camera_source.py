"""`best_camera_source()`: the right video source for whatever this machine has - the Pi's hardware encoder, else the Pi camera encoded on the CPU (a Pi 5), else a test
pattern - and an ERROR, not a test pattern, on a Raspberry Pi that cannot use its camera. `PiCameraSource.available()` and `running_on_a_pi()` are what it decides on."""

from __future__ import annotations

import types

import pytest

pytest.importorskip("av")  # the encoded test pattern needs PyAV

from pihub.video import (  # noqa: E402
    CameraUnavailable,
    EncodedTestPatternSource,
    HardwareCameraSource,
    PiCameraSource,
    SoftwareEncodedCameraSource,
    best_camera_source,
)
from pihub.video import best as best_module  # noqa: E402
from pihub.video import sources  # noqa: E402


@pytest.fixture
def machine(monkeypatch):
    """Say what this machine has: `machine(hardware=True, camera=True, pi=True)`. Nothing here needs a Pi."""

    def set_up(*, hardware: bool = False, camera: bool = False, pi: bool = False, libx264: bool = False) -> None:
        monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: hardware))
        monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: camera and libx264))
        monkeypatch.setattr(PiCameraSource, "available", staticmethod(lambda: camera))
        monkeypatch.setattr(best_module, "running_on_a_pi", lambda: pi)

    return set_up


# -- the ladder ------------------------------------------------------------------------------------------------------


def test_a_pi_with_the_hardware_encoder_uses_it(machine):
    machine(hardware=True, camera=True, pi=True)
    source = best_camera_source(size=(800, 600), fps=10, bitrate=2_000_000, tuning="imx219_noir")

    assert type(source) is HardwareCameraSource
    assert source.encoded is True
    assert (source.width, source.height, source.fps) == (800, 600, 10)
    assert source.bitrate == 2_000_000
    assert source.tuning == "imx219_noir"
    assert "hardware" in source.description


def test_a_pi_with_no_hardware_encoder_and_no_libx264_falls_back_to_the_raw_camera(machine):
    """The last resort before an error: the camera's raw frames, which `aiortc` encodes. It streams and takes snapshots, but cannot be recorded. (With libx264, the
    recordable software source is chosen instead: see `test_software_encoded_source.py`.)"""
    machine(hardware=False, camera=True, pi=True, libx264=False)
    source = best_camera_source(size=(640, 480), fps=15, tuning="imx219_noir")

    assert type(source) is PiCameraSource
    assert source.encoded is False  # so it can stream and take snapshots, but not be recorded
    assert source.real_camera is True
    assert source.tuning == "imx219_noir"
    assert "software" in source.description


def test_software_encoder_skips_the_hardware_encoder_even_when_there_is_one(machine):
    machine(hardware=True, camera=True, pi=True, libx264=False)
    assert type(best_camera_source(software_encoder=True)) is PiCameraSource
    machine(hardware=True, camera=True, pi=True, libx264=True)
    assert type(best_camera_source(software_encoder=True)) is SoftwareEncodedCameraSource


def test_a_computer_with_no_camera_gets_an_encoded_test_pattern(machine):
    machine(hardware=False, camera=False, pi=False)
    source = best_camera_source(size=(320, 240), fps=5)

    assert type(source) is EncodedTestPatternSource
    assert source.encoded is True  # as the hardware's is, so it can be recorded too
    assert source.real_camera is False
    assert (source.width, source.height, source.fps) == (320, 240, 5)
    assert "test pattern" in source.description


def test_test_pattern_forces_the_pattern_even_on_a_pi_with_a_camera(machine):
    machine(hardware=True, camera=True, pi=True)
    assert type(best_camera_source(test_pattern=True)) is EncodedTestPatternSource


# -- a Pi that cannot use its camera is an error, never a test pattern -------------------------------------------------


def test_a_pi_with_no_usable_camera_is_an_error_that_says_what_to_check(machine):
    machine(hardware=False, camera=False, pi=True)

    with pytest.raises(CameraUnavailable) as error:
        best_camera_source()

    message = str(error.value)
    assert "no usable camera" in message
    assert "ribbon cable" in message
    assert "python3-picamera2" in message
    assert "--system-site-packages" in message


def test_software_encoder_on_a_pi_with_no_camera_library_is_the_same_error(machine):
    machine(hardware=True, camera=False, pi=True)
    with pytest.raises(CameraUnavailable):
        best_camera_source(software_encoder=True)


# -- what it decides on --------------------------------------------------------------------------------------------------


def _picamera2(monkeypatch, cameras):
    class Picamera2:
        @staticmethod
        def global_camera_info():
            if isinstance(cameras, Exception):
                raise cameras
            return cameras

    monkeypatch.setattr(sources, "import_picamera2", lambda: types.SimpleNamespace(Picamera2=Picamera2))


def test_pi_camera_source_is_available_when_the_library_lists_a_camera(monkeypatch):
    _picamera2(monkeypatch, [{"Model": "imx219"}])
    assert PiCameraSource.available() is True


def test_pi_camera_source_is_not_available_with_no_camera_attached(monkeypatch):
    _picamera2(monkeypatch, [])
    assert PiCameraSource.available() is False


def test_pi_camera_source_is_not_available_when_the_library_cannot_say(monkeypatch):
    _picamera2(monkeypatch, RuntimeError("libcamera is not running"))
    assert PiCameraSource.available() is False


def test_pi_camera_source_is_not_available_without_the_library(monkeypatch):
    def missing():
        raise CameraUnavailable("picamera2 is not installed")

    monkeypatch.setattr(sources, "import_picamera2", missing)
    assert PiCameraSource.available() is False


@pytest.mark.parametrize(("model", "expected"), [(b"Raspberry Pi 5 Model B Rev 1.0\x00", True), (b"Raspberry Pi Zero W Rev 1.1\x00", True), (b"Some Other Board\x00", False)])
def test_running_on_a_pi_reads_the_device_tree(tmp_path, monkeypatch, model, expected):
    path = tmp_path / "model"
    path.write_bytes(model)
    monkeypatch.setattr(sources, "PI_MODEL_FILE", str(path))
    assert sources.running_on_a_pi() is expected


def test_running_on_a_pi_is_false_where_there_is_no_device_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "PI_MODEL_FILE", str(tmp_path / "missing"))
    assert sources.running_on_a_pi() is False


def test_every_source_says_in_a_few_words_what_it_is():
    for source_class in (HardwareCameraSource, PiCameraSource, EncodedTestPatternSource):
        assert isinstance(source_class.description, str) and 8 < len(source_class.description) < 80
    assert len({HardwareCameraSource.description, PiCameraSource.description, EncodedTestPatternSource.description}) == 3
