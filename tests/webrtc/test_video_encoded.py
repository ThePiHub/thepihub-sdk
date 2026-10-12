"""The hardware-encoder path: video that is ALREADY H.264 when it reaches aiortc, which sends it without encoding.

`EncodedTestPatternSource` stands in for the Pi's hardware encoder (it encodes in the source, as the hardware does on the
GPU), so the pass-through, the keyframe handling and the codec rules are tested anywhere. `HardwareCameraSource` itself
is tested against a fake `picamera2`; the real hardware is measured on a real Pi.
"""

import asyncio
import re
import sys
import threading
import time
import types

import pytest

av = pytest.importorskip("av")
aiortc = pytest.importorskip("aiortc")

from pihub.protocol.messages import SIGNAL_CLOSE, SignalMessage  # noqa: E402
from pihub.video import CameraUnavailable, EncodedTestPatternSource, HardwareCameraSource, PiCameraSource, capture_still  # noqa: E402
from pihub.video import hardware  # noqa: E402
from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402
from tests.webrtc.test_peer_manager import FakeClock, Wire, run, until  # noqa: E402
from tests.webrtc.test_video import (  # noqa: E402
    STREAM,
    grant_media,
    offer_video,
    signal_on_channel,
    start_session,
)


def nal_types(data: bytes):
    """The H.264 NAL unit types in an Annex B buffer: 7 = SPS, 8 = PPS, 5 = a keyframe, 1 = a delta frame."""
    types_, i = [], 0
    while True:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(data):
            return types_
        types_.append(data[j + 3] & 0x1F)
        i = j + 3


class Recorder(EncodedTestPatternSource):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.opens = self.closes = 0

    def _open(self):
        self.opens += 1
        super()._open()

    def _close(self):
        self.closes += 1
        super()._close()


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def source():
    return Recorder(size=(640, 480), fps=15)


@pytest.fixture
def manager(wire, source):
    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=2, max_media_sessions=1), clock=FakeClock(), heartbeat_seconds=0.2)
    peers.set_video_sources({STREAM: source})
    peers.start()
    yield peers
    peers.stop()


# --- the stand-in source ------------------------------------------------------------------------------------------------


def test_the_encoded_test_source_yields_annex_b_h264_packets_with_headers_on_keyframes():
    source = EncodedTestPatternSource(fps=15)
    source.start()
    try:
        packets = [source.read() for _ in range(20)]
    finally:
        source.stop()
    assert source.encoded and all(isinstance(p, av.Packet) for p in packets)
    first = nal_types(bytes(packets[0]))
    assert 7 in first and 8 in first and 5 in first  # SPS, PPS and an IDR picture: a viewer can start here
    assert 1 in nal_types(bytes(packets[3]))  # then delta frames
    assert 7 in nal_types(bytes(packets[15]))  # and the headers repeat with the next keyframe (about a second later)


def test_a_keyframe_can_be_requested_on_demand():
    source = EncodedTestPatternSource(fps=15)
    source.start()
    try:
        [source.read() for _ in range(4)]
        assert 5 not in nal_types(bytes(source.read()))  # mid-GOP: a delta frame
        source.request_keyframe()
        forced = [nal_types(bytes(source.read())) for _ in range(3)]
    finally:
        source.stop()
    assert any(5 in types_ for types_ in forced) and source.keyframes_requested == 1


def test_an_encoded_packet_is_not_remembered_as_a_snapshot_frame():
    source = EncodedTestPatternSource()
    source.start()
    try:
        source.read()
        assert source.latest_frame() is None  # a packet is not a picture
        still = source.grab_still()
        assert (still.width, still.height) == (640, 480)  # a raw picture, made separately
    finally:
        source.stop()


# --- aiortc sends it without encoding ---------------------------------------------------------------------------------------


def test_pre_encoded_h264_reaches_a_real_browser_and_aiortc_never_encodes_it(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            answer, frames, got = await offer_video(pc, channel, inbox)
            assert answer is not None, signal_on_channel(inbox, "reject")
            await asyncio.wait_for(got.wait(), 15)
            await until(lambda: len(frames) >= 20, timeout=40, what="20 decoded frames")
            sender = manager._grants["s1"].pc.getSenders()[0]
            encoder = sender._RTCRtpSender__encoder
            return answer, frames, encoder
        finally:
            await pc.close()

    answer, frames, encoder = run(scenario())
    assert (frames[-1].width, frames[-1].height) == (640, 480)
    video_line = next(l for l in answer["sdp"].splitlines() if l.startswith("m=video"))
    payload = video_line.split()[3]
    assert "H264" in next(l for l in answer["sdp"].splitlines() if l.startswith(f"a=rtpmap:{payload} "))
    # THE point of the path: aiortc's own H.264 encoder was created but never encoded a frame (its codec is never built).
    assert encoder is None or getattr(encoder, "codec", None) is None


def test_release_and_restart_work_with_an_encoded_source(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 5, timeout=40, what="first frames")
            from pihub.protocol.messages import SIGNAL_MEDIA

            manager.handle_signal(SignalMessage(SIGNAL_MEDIA, "s1", {"action": "release"}))
            await until(lambda: source.closes >= 1, what="the source to close")
            seen = len(frames)
            await grant_media(manager, wire, count=2)
            answer2, frames2, _ = await offer_video(pc, channel, inbox)
            assert answer2 is not None, signal_on_channel(inbox, "reject")
            await until(lambda: len(frames2) >= seen + 5, timeout=40, what="new frames after the restart")
            return source.opens
        finally:
            await pc.close()

    assert run(scenario()) == 2


def test_an_offer_with_no_h264_is_refused_before_the_camera_starts(manager, wire, source):
    """Pre-encoded H.264 cannot become VP8, so a browser that will not take H.264 is refused up front."""

    async def scenario():
        from aiortc import RTCRtpSender

        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            transceiver = pc.addTransceiver("video", direction="recvonly")
            vp8 = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType.lower() == "video/vp8"]
            transceiver.setCodecPreferences(vp8)
            await pc.setLocalDescription(await pc.createOffer())
            from pihub.protocol.messages import SIGNAL_OFFER

            channel.send(SignalMessage(SIGNAL_OFFER, "s1", {"sdp": pc.localDescription.sdp}).to_json())
            await until(lambda: signal_on_channel(inbox, "reject"), what="the refusal")
            return signal_on_channel(inbox, "reject")[0]["reason"]
        finally:
            await pc.close()

    assert run(scenario()) == "codec_unsupported"
    assert source.opens == 0  # the camera never started


def test_a_browsers_request_for_a_keyframe_reaches_the_encoder(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            answer, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 5, timeout=40, what="frames")
            before = source.keyframes_requested  # one was already asked for when the viewer started
            assert before >= 1
            # The browser side asks for a keyframe, exactly as a browser does after packet loss (RTCP PLI).
            receiver = next(r for r in pc.getReceivers() if r.track.kind == "video")
            ssrc = int(re.search(r"a=ssrc:(\d+) ", answer["sdp"]).group(1))
            await receiver._send_rtcp_pli(ssrc)
            await until(lambda: source.keyframes_requested > before, timeout=10, what="the request to reach the encoder")
        finally:
            await pc.close()

    run(scenario())


def test_a_snapshot_during_encoded_video_is_a_real_picture_and_the_stream_carries_on(manager, wire, source, tmp_path):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 10, timeout=40, what="frames")
            path = await asyncio.get_running_loop().run_in_executor(None, capture_still, source, tmp_path)
            seen = len(frames)
            await until(lambda: len(frames) >= seen + 10, timeout=40, what="the video to carry on")
            return path, (source.opens, source.closes)
        finally:
            await pc.close()

    path, counts = run(scenario())
    assert path.stat().st_size > 0 and counts == (1, 0)  # a picture, and the camera was shared, not reopened


def test_the_camera_stops_when_an_encoded_viewer_vanishes(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        await grant_media(manager, wire)
        _, frames, _ = await offer_video(pc, channel, inbox)
        await until(lambda: len(frames) >= 5, timeout=40, what="frames")
        await pc.close()
        await until(lambda: source.closes >= 1, timeout=40, what="the encoder to stop")

    run(scenario())
    assert not source.running


# --- the hardware source, against a fake picamera2 ---------------------------------------------------------------------------


class FakeEncoder:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.vd = object()
        FakeEncoder.instances.append(self)


class FakeOutput:
    def __init__(self):
        self.recording = False

    def start(self):
        self.recording = True


class FakeCamera:
    instances = []

    def __init__(self, tuning=None):
        self.tuning = tuning
        self.configured = None
        self.recording = None
        self.captures = 0
        self.stopped = self.closed = 0
        FakeCamera.instances.append(self)

    @staticmethod
    def load_tuning_file(tuning_file, dir=None):
        return {"loaded": tuning_file, "dir": dir}

    def create_video_configuration(self, main=None, controls=None):
        return {"main": main, "controls": controls}

    def configure(self, config):
        self.configured = config

    def start_recording(self, encoder, output):
        output.start()
        self.recording = (encoder, output)

    def start(self):  # the plain (software-encoded) source starts the camera without an encoder
        pass

    def stop(self):
        self.stopped += 1

    def stop_recording(self):
        self.stopped += 1

    def close(self):
        self.closed += 1

    def capture_array(self, name):
        import numpy as np

        self.captures += 1
        return np.full((480 * 3 // 2, 640), 100, dtype="uint8")


@pytest.fixture(autouse=True)
def no_saved_camera_setting(tmp_path, monkeypatch):
    """A camera.json on the machine running the tests must never leak into them."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.fixture
def fake_picamera2(monkeypatch):
    FakeEncoder.instances, FakeCamera.instances = [], []
    module = types.ModuleType("picamera2")
    module.Picamera2 = FakeCamera
    encoders = types.ModuleType("picamera2.encoders")
    encoders.H264Encoder = FakeEncoder
    outputs = types.ModuleType("picamera2.outputs")
    outputs.Output = FakeOutput
    videodev2 = types.ModuleType("videodev2")
    videodev2.V4L2_CID_MPEG_VIDEO_FORCE_KEY_FRAME = 0x9909CB
    videodev2.VIDIOC_S_CTRL = 0xC008561C

    class Control:
        id = value = 0

    videodev2.v4l2_control = Control
    for name, mod in (("picamera2", module), ("picamera2.encoders", encoders), ("picamera2.outputs", outputs), ("videodev2", videodev2)):
        monkeypatch.setitem(sys.modules, name, mod)
    calls = []
    import fcntl

    monkeypatch.setattr(fcntl, "ioctl", lambda fd, request, arg: calls.append((request, arg.id, arg.value)))
    return types.SimpleNamespace(calls=calls)


def test_the_hardware_source_configures_the_camera_and_encoder_as_designed(fake_picamera2):
    source = HardwareCameraSource(size=(640, 480), fps=15, bitrate=800_000, keyframe_seconds=1.0)
    source.start()
    try:
        camera, encoder = FakeCamera.instances[0], FakeEncoder.instances[0]
        assert camera.configured["main"] == {"size": (640, 480), "format": "YUV420"}
        assert camera.configured["controls"] == {"FrameRate": 15}
        assert encoder.kwargs == {
            "bitrate": 800_000,
            "repeat": True,  # headers repeated with every keyframe, so a viewer can join at any of them
            "iperiod": 15,  # a keyframe every second at 15 fps
            "framerate": 15,
            "profile": "constrained baseline",
        }
        assert source.encoded and source.running
    finally:
        source.stop()
    assert camera.stopped == 1 and camera.closed == 1 and not source.running


def test_a_saved_camera_setting_is_used_when_no_tuning_is_given(fake_picamera2):
    from pihub.camera_config import save_camera_tuning

    save_camera_tuning("imx219_noir")  # what `pihub camera setup` saves
    for source in (HardwareCameraSource(), PiCameraSource()):
        source.start()
        source.stop()
    assert [camera.tuning for camera in FakeCamera.instances] == [{"loaded": "imx219_noir.json", "dir": None}] * 2


def test_an_explicit_tuning_beats_the_saved_one_and_an_empty_one_means_the_cameras_own(fake_picamera2):
    from pihub.camera_config import save_camera_tuning

    save_camera_tuning("imx219_noir")
    for source in (HardwareCameraSource(tuning="imx477_noir"), HardwareCameraSource(tuning="")):
        source.start()
        source.stop()
    assert [camera.tuning for camera in FakeCamera.instances] == [{"loaded": "imx477_noir.json", "dir": None}, None]


def test_no_tuning_leaves_the_camera_on_its_own_default(fake_picamera2):
    for source in (HardwareCameraSource(), PiCameraSource()):
        source.start()
        source.stop()
    assert [camera.tuning for camera in FakeCamera.instances] == [None, None]


@pytest.mark.parametrize(
    "given, loaded",
    [
        ("imx219_noir", {"loaded": "imx219_noir.json", "dir": None}),  # a name: .json is added, libcamera's folders are searched
        ("imx219_noir.json", {"loaded": "imx219_noir.json", "dir": None}),
        ("/home/pi/my_tuning.json", {"loaded": "my_tuning.json", "dir": "/home/pi"}),  # a path is split into folder and file
    ],
)
@pytest.mark.parametrize("make", [HardwareCameraSource, PiCameraSource])
def test_a_tuning_is_loaded_and_given_to_the_camera(fake_picamera2, make, given, loaded):
    """A NoIR camera looks strongly pink under the default tuning (measured: red 5.6x green); its own tuning fixes that."""
    source = make(tuning=given)
    source.start()
    source.stop()
    assert FakeCamera.instances[0].tuning == loaded


def test_packets_from_the_encoder_come_out_of_read_as_av_packets_and_are_copied(fake_picamera2):
    source = HardwareCameraSource()
    source.start()
    try:
        output = FakeCamera.instances[0].recording[1]
        buffer = bytearray(b"\x00\x00\x00\x01\x67abc")
        output.outputframe(buffer, keyframe=True)
        buffer[:] = b"XXXXXXXXXX"  # the encoder reuses its buffer: what was queued must not change
        packet = source.read()
        assert isinstance(packet, av.Packet) and bytes(packet) == b"\x00\x00\x00\x01\x67abc"
    finally:
        source.stop()


def test_an_encoder_that_produces_nothing_is_a_clear_error(fake_picamera2, monkeypatch):
    monkeypatch.setattr(hardware, "READ_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(hardware, "STARTUP_FRAMES", 0)  # so the wait is the 0.1 s above, not a dozen frames' worth
    source = HardwareCameraSource()
    source.start()
    try:
        with pytest.raises(CameraUnavailable) as raised:
            source.read()
        assert "no video" in str(raised.value)
    finally:
        source.stop()


def test_the_wait_for_video_grows_as_the_frame_rate_falls():
    # A dozen frames at 2 fps is 6 s: the camera took about 8 s to show its first picture at 2 fps (Zero W), and a fixed 2 s gave up and left it black.
    assert hardware.read_timeout_seconds(15) == 2.0
    assert hardware.read_timeout_seconds(5) == pytest.approx(2.4)
    assert hardware.read_timeout_seconds(2) == 6.0
    assert hardware.read_timeout_seconds(1) == 12.0


def test_a_low_frame_rate_source_waits_that_long_for_a_packet():
    class Buffer:
        timeout = None

        def get(self, timeout):
            self.timeout = timeout
            return b"\x00\x00\x00\x01\x67abc", True

    source = HardwareCameraSource(fps=2)
    source._buffer = Buffer()
    source._read()
    assert source._buffer.timeout == 6.0


def test_stopping_wakes_a_blocked_reader_at_once(fake_picamera2):
    source = HardwareCameraSource()
    source.start()
    outcome = {}

    def reader():
        try:
            source.read()
        except CameraUnavailable as error:
            outcome["error"] = str(error)

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.2)
    started = time.monotonic()
    source.stop()
    thread.join(timeout=1.5)
    assert not thread.is_alive() and "stopped" in outcome["error"]
    assert time.monotonic() - started < 1.0  # not the 2 s a read would otherwise wait


def test_a_camera_that_will_not_start_is_a_clear_error_and_holds_nothing(fake_picamera2, monkeypatch):
    def boom(self, encoder, output):
        raise RuntimeError("Camera __init__ sequence did not complete.")

    monkeypatch.setattr(FakeCamera, "start_recording", boom)
    source = HardwareCameraSource()
    with pytest.raises(CameraUnavailable) as raised:
        source.start()
    assert "hardware encoder" in str(raised.value) and not source.running and source._users == 0


def test_a_keyframe_request_is_a_force_key_frame_control_on_the_encoder(fake_picamera2):
    source = HardwareCameraSource()
    source.request_keyframe()  # not started: nothing to ask, and no error
    assert fake_picamera2.calls == []
    source.start()
    try:
        source.request_keyframe()
    finally:
        source.stop()
    assert fake_picamera2.calls == [(0xC008561C, 0x9909CB, 1)]


def test_a_snapshot_captures_beside_the_encoder_and_settles_only_when_the_camera_is_new(fake_picamera2, tmp_path):
    source = HardwareCameraSource()
    source.start()
    try:
        camera = FakeCamera.instances[0]
        first = capture_still(source, tmp_path)
        assert camera.captures == hardware.SETTLE_FRAMES  # a new camera: let exposure settle
        source._opened_at -= 10  # ...and a camera that has been running a while needs only one
        second = capture_still(source, tmp_path)
        assert camera.captures == hardware.SETTLE_FRAMES + 1
        assert source.running  # the stream's hold on the camera survived both snapshots
    finally:
        source.stop()
    assert first.exists() and second.exists() and first != second


def test_availability_needs_both_the_encoder_device_and_the_library(monkeypatch, fake_picamera2):
    monkeypatch.setattr(hardware.os.path, "exists", lambda path: False)
    assert HardwareCameraSource.available() is False  # a machine with no hardware encoder (a Pi 5, a laptop)
    monkeypatch.setattr(hardware.os.path, "exists", lambda path: True)
    assert HardwareCameraSource.available() is True
    monkeypatch.setitem(sys.modules, "picamera2", None)
    assert HardwareCameraSource.available() is False  # the device is there but the library is not


def test_the_keyframe_interval_must_be_positive():
    with pytest.raises(ValueError):
        HardwareCameraSource(keyframe_seconds=0)


# --- back-pressure: an encoded stream cannot be thinned, so overflow waits for a keyframe ----------------------------------------


def test_a_buffer_that_overflows_empties_itself_and_waits_for_the_next_keyframe():
    asked = []
    buffer = hardware.PacketBuffer(maxsize=3, on_overflow=lambda: asked.append(True))
    buffer.put(b"key", True)
    buffer.put(b"d1", False)
    buffer.put(b"d2", False)
    buffer.put(b"d3", False)  # full: everything goes, and a keyframe is requested
    assert asked == [True] and buffer.dropped == 4

    buffer.put(b"d4", False)  # a delta frame cannot start a picture: skipped
    buffer.put(b"d5", False)
    assert buffer.dropped == 6
    buffer.put(b"key2", True)  # the keyframe resumes the stream
    buffer.put(b"d6", False)
    assert buffer.get(0.1) == (b"key2", True) and buffer.get(0.1) == (b"d6", False)


def test_a_buffer_in_order_keeps_everything():
    buffer = hardware.PacketBuffer(maxsize=10)
    for i in range(5):
        buffer.put(bytes([i]), i == 0)
    assert [buffer.get(0.1)[0] for _ in range(5)] == [bytes([i]) for i in range(5)] and buffer.dropped == 0


def test_negative_control_a_raw_source_IS_encoded_by_aiortc(wire):
    """Proves the 'aiortc never encodes' check above discriminates: with a RAW source aiortc builds its encoder."""
    from pihub.video import TestPatternSource

    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=2, max_media_sessions=1), clock=FakeClock(), heartbeat_seconds=0.2)
    peers.set_video_sources({STREAM: TestPatternSource()})
    peers.start()
    try:
        async def scenario():
            pc, channel, inbox = await start_session(peers, wire)
            try:
                await grant_media(peers, wire)
                _, frames, _ = await offer_video(pc, channel, inbox)
                await until(lambda: len(frames) >= 10, timeout=40, what="frames")
                return peers._grants["s1"].pc.getSenders()[0]._RTCRtpSender__encoder
            finally:
                await pc.close()

        encoder = run(scenario())
    finally:
        peers.stop()
    assert encoder is not None and encoder.codec is not None  # aiortc really did encode these frames
