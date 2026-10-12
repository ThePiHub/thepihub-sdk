"""`SoftwareEncodedCameraSource`: a Pi camera encoded to H.264 by the CPU (libx264) and handed on as packets, so a Pi with no hardware encoder (a Pi 5) can RECORD.

No Pi here: a fake picamera2 stands in for the camera and its encoder, and the packets it pushes are REAL libx264 output, so a recording made from them is decoded to prove the file
plays. What only a real Pi can show (the CPU, the frame rate, a browser decoding the stream) is in the backlog's Pi 5 checklist."""

from __future__ import annotations

import threading
import time
import types
from fractions import Fraction
from pathlib import Path
from typing import Any, List

import pytest

av = pytest.importorskip("av")

from pihub.video import hardware, software  # noqa: E402
from pihub.video import HardwareCameraSource, PiCameraSource, SoftwareEncodedCameraSource, best_camera_source  # noqa: E402
from pihub.video import best as best_module  # noqa: E402
from pihub.video.recording import Recorder, RecordingStore  # noqa: E402

SIZE, FPS = (320, 240), 15
REAL_LIBX264_CHECK = software.libx264_available  # the real function, kept before any test replaces it


def real_packets(seconds: float):
    """(data, keyframe) pairs from real libx264: Annex B, a keyframe a second with the headers repeated, no B-frames (what the encoder is asked for)."""
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height, codec.pix_fmt = SIZE[0], SIZE[1], "yuv420p"
    codec.time_base, codec.framerate = Fraction(1, FPS), FPS
    codec.options = {"tune": "zerolatency", "x264-params": f"repeat-headers=1:keyint={FPS}:min-keyint={FPS}:scenecut=0"}
    packets = []
    for n in range(int(seconds * FPS)):
        frame = av.VideoFrame(SIZE[0], SIZE[1], "yuv420p")
        frame.planes[0].update(bytes((x + n * 7) % 256 for x in range(SIZE[0])) * SIZE[1])
        frame.pts = n
        frame.pict_type = av.video.frame.PictureType.NONE
        packets += [(bytes(p), bool(p.is_keyframe)) for p in codec.encode(frame)]
    packets += [(bytes(p), bool(p.is_keyframe)) for p in codec.encode(None)]
    return packets


def decodable_frames(path: Path) -> int:
    count = 0
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            try:
                for packet in container.demux(stream):
                    count += len(stream.codec_context.decode(packet))
            except av.error.FFmpegError:
                pass
            count += len(stream.codec_context.decode(None))
    except av.error.FFmpegError:
        pass
    return count


def wait_for(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    raise AssertionError("timed out")


class Notes:
    def __init__(self):
        self.sent: List[Any] = []

    def info(self, message):
        self.sent.append(("info", message))

    def warning(self, message):
        self.sent.append(("warning", message))


# -- a fake picamera2 ---------------------------------------------------------------------------------------------------


class FakeOutput:
    """picamera2's `Output` base: the source subclasses it, and the encoder calls `outputframe` on it once `recording` is set."""

    def __init__(self):
        self.recording = False


class FakeEncoder:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.forced = 0

    def force_key_frame(self):
        self.forced += 1


class FakeLibavEncoder(FakeEncoder):
    """picamera2's `LibavH264Encoder`, as far as the source uses it."""


class FakeHardwareEncoder(FakeEncoder):
    pass


class FakePicamera2:
    """A camera whose `start_recording` makes the encoder push real H.264 packets into the output, paced like a camera."""

    made: List["FakePicamera2"] = []
    seconds_of_video = 3.0
    cameras = [{"Model": "imx708"}]

    def __init__(self, *args, **kwargs):
        self.encoder = None
        self.output = None
        self.configured = None
        self.stopped = False
        self.closed = False
        self._thread = None
        FakePicamera2.made.append(self)

    @staticmethod
    def global_camera_info():
        return list(FakePicamera2.cameras)

    def create_video_configuration(self, **kwargs):
        return kwargs

    def configure(self, config):
        self.configured = config

    def start_recording(self, encoder, output):
        self.encoder, self.output = encoder, output
        output.recording = True
        packets = real_packets(FakePicamera2.seconds_of_video)

        def push():
            for index, (data, keyframe) in enumerate(packets):
                if self.stopped:
                    return
                output.outputframe(data, keyframe, int(index * 1_000_000 / FPS))
                time.sleep(1 / FPS)

        self._thread = threading.Thread(target=push, daemon=True)
        self._thread.start()

    def stop_recording(self):
        self.stopped = True
        if self.output is not None:
            self.output.recording = False

    def close(self):
        self.closed = True


def fake_library(libav: bool = True):
    return types.SimpleNamespace(
        Picamera2=FakePicamera2,
        H264Encoder=FakeHardwareEncoder,
        LibavH264Encoder=FakeLibavEncoder if libav else None,
        Output=FakeOutput,
    )


@pytest.fixture(autouse=True)
def fake_picamera2(monkeypatch):
    FakePicamera2.made.clear()
    FakePicamera2.cameras = [{"Model": "imx708"}]
    FakePicamera2.seconds_of_video = 3.0
    library = fake_library()
    monkeypatch.setattr(software, "import_picamera2", lambda: library)
    monkeypatch.setattr(hardware, "import_picamera2", lambda: library)
    monkeypatch.setattr(software, "libx264_available", lambda: True)
    return library


@pytest.fixture
def source():
    camera = SoftwareEncodedCameraSource(size=SIZE, fps=FPS, tuning="")  # tuning "": the camera's own, so no saved setting is read
    yield camera
    while camera.running:
        camera.stop()


# -- what it is ---------------------------------------------------------------------------------------------------------


def test_it_is_an_encoded_real_camera_that_says_what_it_is():
    camera = SoftwareEncodedCameraSource(size=SIZE, fps=FPS)

    assert camera.encoded is True  # so a stream sends its packets as they are, and a recorder can write them
    assert camera.real_camera is True
    assert isinstance(camera, HardwareCameraSource)  # everything but the encoder is shared
    assert "software" in camera.description and "recordable" in camera.description
    assert camera.max_pixels == 640 * 480  # until a Pi 5 has been measured
    assert HardwareCameraSource.max_pixels is None


# -- available() --------------------------------------------------------------------------------------------------------


def test_available_with_the_encoder_a_camera_and_libx264():
    assert SoftwareEncodedCameraSource.available() is True


def test_not_available_with_an_older_picamera2_that_has_no_software_encoder(monkeypatch):
    monkeypatch.setattr(software, "import_picamera2", lambda: fake_library(libav=False))
    assert SoftwareEncodedCameraSource.available() is False


def test_not_available_with_no_camera_attached():
    FakePicamera2.cameras = []
    assert SoftwareEncodedCameraSource.available() is False


def test_not_available_without_libx264(monkeypatch):
    monkeypatch.setattr(software, "libx264_available", lambda: False)
    assert SoftwareEncodedCameraSource.available() is False


def test_not_available_when_the_camera_library_is_missing(monkeypatch):
    def missing():
        raise RuntimeError("picamera2 is not installed")

    monkeypatch.setattr(software, "import_picamera2", missing)
    assert SoftwareEncodedCameraSource.available() is False


def test_the_libx264_check_itself_says_yes_where_pyav_has_it_and_no_where_it_does_not(monkeypatch):
    """The real check, not the fake the other tests use: PyAV here has libx264, and PyAV refusing the codec, or missing, is a no."""
    assert REAL_LIBX264_CHECK() is True

    def refuse(name, mode):
        raise av.codec.codec.UnknownCodecError(name)

    monkeypatch.setattr(av.codec, "Codec", refuse)
    assert REAL_LIBX264_CHECK() is False


# -- the encoder it asks picamera2 for ---------------------------------------------------------------------------------


def test_it_asks_for_the_software_encoder_with_the_hardware_source_s_settings(source):
    source.start()

    [camera] = FakePicamera2.made
    assert isinstance(camera.encoder, FakeLibavEncoder) and not isinstance(camera.encoder, FakeHardwareEncoder)
    assert camera.encoder.kwargs == {
        "bitrate": 1_000_000,
        "repeat": True,  # sequence headers with every keyframe
        "iperiod": FPS,  # a keyframe a second
        "framerate": FPS,
        "profile": "constrained baseline",  # what a browser is certain to decode
    }
    assert camera.configured["main"] == {"size": SIZE, "format": "YUV420"}  # half the data of RGB


def test_the_hardware_source_still_asks_for_the_hardware_encoder():
    camera_source = HardwareCameraSource(size=SIZE, fps=FPS, tuning="")
    camera_source.start()
    try:
        [camera] = FakePicamera2.made
        assert isinstance(camera.encoder, FakeHardwareEncoder)
    finally:
        camera_source.stop()


def test_a_failure_to_start_names_the_software_encoder(monkeypatch, source):
    def broken(self, config):
        raise RuntimeError("no camera")

    monkeypatch.setattr(FakePicamera2, "configure", broken)
    with pytest.raises(Exception, match="software \\(libx264\\) encoder"):
        source.start()


# -- packets, listeners, keyframes ------------------------------------------------------------------------------------


def test_packets_reach_a_reader_and_a_packet_listener(source):
    heard = []
    source.add_packet_listener(lambda data, keyframe, timestamp: heard.append((data, keyframe, timestamp)))
    source.start()

    packet = source.read()

    assert isinstance(packet, av.Packet) and bytes(packet)[:4] == b"\x00\x00\x00\x01"  # Annex B
    wait_for(lambda: len(heard) >= 5)
    assert heard[0][1] is True  # the first packet is a keyframe
    assert heard[0][2] == 0


def test_it_never_forces_a_keyframe_because_that_breaks_the_stream_on_a_real_pi(source):
    """picamera2's `force_key_frame()` set `frame.pict_type = "I"`, which PyAV 14.2.0 rejects inside picamera2's own thread: on a real Pi 3A+ the encoder stopped and
    the recording that followed got no video. So this source lets the regular keyframes (a second apart) do the work."""
    source.start()
    source.request_keyframe()
    source.request_keyframe()

    assert FakePicamera2.made[0].encoder.forced == 0
    assert HardwareCameraSource.request_keyframe is not SoftwareEncodedCameraSource.request_keyframe  # not the hardware source's V4L2 ioctl either


def test_asking_for_a_keyframe_before_it_is_started_is_harmless():
    SoftwareEncodedCameraSource().request_keyframe()


def test_stopping_closes_the_camera(source):
    source.start()
    source.stop()

    camera = FakePicamera2.made[0]
    assert camera.stopped and camera.closed


# -- recording: the point of it ---------------------------------------------------------------------------------------


def test_it_can_be_recorded_to_a_file_that_plays(source, tmp_path):
    """The reason for the source: a Pi 5 has no hardware encoder, and `PiCameraSource` (raw frames) cannot be recorded. This one can."""
    store = RecordingStore(tmp_path, free_bytes=lambda _: 10**12)
    recorder = Recorder(source, store=store, notify=Notes(), fsync=False)

    assert recorder.start(reason="test") is True
    wait_for(lambda: recorder.current is not None and Path(recorder.current.path).exists())
    time.sleep(1.8)
    recording = recorder.stop()

    path = Path(recording.path)
    assert decodable_frames(path) == recording.frames >= 15
    assert not source.running  # the camera closed again


def test_a_raw_pi_camera_source_still_cannot_be_recorded(tmp_path):
    """The contrast: this is why `PiCameraSource` alone leaves a Pi 5 with no Record button."""
    with pytest.raises(ValueError, match="encoded source"):
        Recorder(PiCameraSource(size=SIZE, fps=FPS), store=RecordingStore(tmp_path, free_bytes=lambda _: 10**12), notify=Notes(), fsync=False)


# -- best_camera_source() with it -------------------------------------------------------------------------------------


@pytest.fixture
def machine(monkeypatch):
    """Say what this machine has. `software` is the new rung; `raw` is the Pi camera whose frames aiortc encodes."""

    def set_up(*, hardware_encoder=False, software=False, raw=False, pi=False):
        monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: hardware_encoder))
        monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: software))
        monkeypatch.setattr(PiCameraSource, "available", staticmethod(lambda: raw))
        monkeypatch.setattr(best_module, "running_on_a_pi", lambda: pi)

    return set_up


def test_a_pi_5_now_gets_the_recordable_software_source(machine):
    machine(software=True, raw=True, pi=True)
    chosen = best_camera_source(size=(640, 480), fps=15, bitrate=2_000_000, tuning="imx708")

    assert type(chosen) is SoftwareEncodedCameraSource
    assert chosen.encoded is True
    assert chosen.bitrate == 2_000_000 and chosen.tuning == "imx708"


def test_the_hardware_encoder_is_still_first(machine):
    machine(hardware_encoder=True, software=True, raw=True, pi=True)
    assert type(best_camera_source()) is HardwareCameraSource


def test_software_encoder_forces_the_recordable_software_source_on_a_pi_with_the_hardware_encoder(machine):
    machine(hardware_encoder=True, software=True, raw=True, pi=True)
    assert type(best_camera_source(software_encoder=True)) is SoftwareEncodedCameraSource


def test_without_libx264_a_pi_falls_back_to_the_raw_camera_that_cannot_record(machine):
    machine(software=False, raw=True, pi=True)
    chosen = best_camera_source()

    assert type(chosen) is PiCameraSource and chosen.encoded is False


def test_a_program_that_must_record_gets_an_error_naming_what_to_install_rather_than_a_source_that_cannot(machine):
    from pihub.video import CameraUnavailable

    machine(software=False, raw=True, pi=True)

    with pytest.raises(CameraUnavailable) as error:
        best_camera_source(require_encoded=True)

    assert "python3-av" in str(error.value) and "libx264" in str(error.value)


def test_require_encoded_costs_nothing_where_an_encoded_source_exists(machine):
    machine(software=True, raw=True, pi=True)
    assert best_camera_source(require_encoded=True).encoded is True
    machine(hardware_encoder=True, pi=True)
    assert best_camera_source(require_encoded=True).encoded is True
    machine(pi=False)
    assert best_camera_source(require_encoded=True).encoded is True  # the encoded test pattern
