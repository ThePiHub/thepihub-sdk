"""`Device` and WebRTC: it is an addition. A device without it (or that turns it
off) behaves exactly as before, and one with it announces itself, routes
signalling to the peer manager, and never lets a WebRTC failure reach the
control connection."""

import json
import subprocess
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10, which the SDK supports, has no tomllib: `tomli` is the same thing
    import tomli as tomllib
from pathlib import Path

import pytest

from pihub import Device, DeviceLimits
from pihub.protocol.messages import SIGNAL_OPEN, SignalMessage, parse_message
from pihub.protocol.envelope import Envelope

REPO = Path(__file__).resolve().parents[2]


class FakeTransport:
    def __init__(self):
        self.sent = []
        self.connected = True

    def send(self, data):
        self.sent.append(data)

    def kinds(self):
        out = []
        for raw in list(self.sent):
            message = parse_message(Envelope.from_json(raw))
            out.append(getattr(message, "kind", None) or type(message).__name__)
        return out


def wait_until(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _requirements():
    from packaging.requirements import Requirement

    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    return project, {Requirement(item).name: Requirement(item) for item in project["dependencies"]}


def test_the_plain_install_includes_the_webrtc_library():
    """Direct connections and live video are part of `pip install thepihub` (2026-09-30): nobody should have to know to ask for an extra."""
    _, requirements = _requirements()
    assert set(requirements) == {"websocket-client", "aiortc"}  # gpiozero stays in the `gpio` extra: pins are the Pi's, not everyone's
    assert requirements["aiortc"].marker is not None


def test_the_webrtc_library_is_installed_only_where_pip_has_wheels_for_it():
    """An allow-list, so a 32-bit ARM Pi (PyAV has no wheels there) and any architecture nobody thought of skip it instead of failing to build."""
    _, requirements = _requirements()
    marker = requirements["aiortc"].marker
    for machine in ("x86_64", "AMD64", "aarch64", "arm64"):
        assert marker.evaluate({"platform_machine": machine}), machine
    for machine in ("armv6l", "armv7l", "armv8l", "i686", "riscv64"):  # armv8l: a 32-bit userland on a 64-bit kernel
        assert not marker.evaluate({"platform_machine": machine}), machine


def test_the_old_extras_still_exist_so_existing_install_lines_keep_working():
    project, _ = _requirements()
    extras = project["optional-dependencies"]
    assert {"webrtc", "gpio"} <= set(extras)
    assert any(dep.startswith("aiortc") for dep in extras["webrtc"])
    assert any(dep.startswith("gpiozero") for dep in extras["gpio"])


def test_importing_pihub_does_not_import_the_webrtc_library():
    code = "import sys, pihub; from pihub import Device, DeviceLimits; assert 'aiortc' not in sys.modules, 'aiortc was imported'"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).returncode == 0


def test_a_device_with_a_caller_supplied_transport_does_not_use_webrtc_by_default():
    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport)
    device.start()
    try:
        assert device._peers is None
        assert "capabilities" not in transport.kinds()  # unchanged from before: nothing new on the wire
    finally:
        device.stop()


def test_webrtc_can_be_switched_off_by_the_environment(monkeypatch):
    monkeypatch.setenv("PIHUB_WEBRTC", "0")
    device = Device(id="birdbox01", transport=FakeTransport(), webrtc=True)
    device.start()
    try:
        assert device._peers is None
    finally:
        device.stop()


def test_requesting_webrtc_without_the_library_warns_and_carries_on(monkeypatch, caplog):
    import pihub.webrtc.peer_manager as peer_manager

    monkeypatch.setattr(peer_manager, "webrtc_available", lambda: False)
    monkeypatch.setattr("pihub.webrtc.webrtc_available", lambda: False)
    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport, webrtc=True)
    with caplog.at_level("WARNING"):
        device.start()
    try:
        assert device._peers is None
        assert any("aiortc" in r.getMessage() and "off" in r.getMessage() for r in caplog.records)
        assert "registration" in transport.kinds()[0].lower() or transport.sent  # the device still started
    finally:
        device.stop()


def test_a_peer_manager_that_cannot_start_never_stops_the_device(monkeypatch, caplog):
    import pihub.webrtc.peer_manager as peer_manager

    def broken(self):
        raise RuntimeError("native library exploded")

    monkeypatch.setattr(peer_manager.PeerManager, "start", broken)
    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport, webrtc=True)
    with caplog.at_level("ERROR"):
        device.start()
    try:
        assert device._peers is None and transport.sent  # registered anyway
    finally:
        device.stop()


def test_a_webrtc_device_announces_itself_and_answers_a_grant():
    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport, webrtc=True, limits=DeviceLimits(max_viewer_sessions=2))
    device.start()
    try:
        assert wait_until(lambda: "resync" in transport.kinds())
        kinds = transport.kinds()
        assert kinds.index("capabilities") < kinds.index("resync")  # capabilities first
        capabilities = next(
            json.loads(raw)["payload"] for raw in transport.sent if json.loads(raw)["payload"].get("kind") == "capabilities"
        )
        assert capabilities["limits"]["maxViewerSessions"] == 2

        # A signal arriving on the control connection reaches the peer manager.
        device.handle_incoming(
            SignalMessage(SIGNAL_OPEN, "s1", {"ttlSeconds": 60, "expiresAt": "x", "grants": ["data"], "iceServers": []}).to_json()
        )
        assert wait_until(lambda: "accept" in transport.kinds())
    finally:
        device.stop()
    assert device._peers is None


def test_signals_are_ignored_by_a_device_without_webrtc():
    device = Device(id="birdbox01", transport=FakeTransport())
    device.start()
    try:
        device.handle_incoming(SignalMessage(SIGNAL_OPEN, "s1", {"ttlSeconds": 60}).to_json())  # must not raise
    finally:
        device.stop()


# --- live video: the developer-facing API ---------------------------------------------------------------------


def test_a_video_output_takes_a_source_and_only_a_video_output_does():
    from pihub import Program
    from pihub.video import TestPatternSource

    program = Program(id="birdbox", name="Bird Box")
    camera = program.output(id="camera", type="Video")
    gauge = program.output(id="temperature", type="Gauge")

    assert camera.use(TestPatternSource()) is camera
    assert camera.stream_id == "birdbox.camera"  # <programId>.<widgetId>: the MediaStream id
    assert list(program.video_sources()) == ["birdbox.camera"]
    with pytest.raises(ValueError):
        gauge.use(TestPatternSource())


def test_a_device_with_a_camera_announces_video_and_publishes_its_stream_id():
    from pihub import Program
    from pihub.video import TestPatternSource

    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport, webrtc=True)
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(TestPatternSource())
    device.add_program(program)
    device.start()
    try:
        assert wait_until(lambda: "resync" in transport.kinds())
        capabilities = next(
            json.loads(raw)["payload"] for raw in transport.sent if json.loads(raw)["payload"].get("kind") == "capabilities"
        )
        assert capabilities["webrtc"]["media"] == ["video"] and capabilities["limits"]["maxMediaSessions"] == 1

        def stream_ids():
            runtime = [json.loads(raw) for raw in transport.sent if json.loads(raw)["messageType"] == "runtime"]
            return [m for m in runtime if m["payload"].get("value") == {"streamId": "birdbox.camera"}]

        # Demand-driven uplink: nobody is watching, so the platform is not sent it ...
        time.sleep(0.3)
        assert stream_ids() == []
        assert {"streamId": "birdbox.camera"} in [m.value for m in device._runtime_snapshot()]  # ... but it is kept, for whoever comes

        # ... and the moment a viewer is granted the platform is brought up to date (the page needs the id to show the video).
        from pihub.protocol.messages import SIGNAL_OPEN, SignalMessage

        device._peers.handle_signal(SignalMessage(SIGNAL_OPEN, "s1", {"expiresAt": "2026-09-19T12:00:00Z", "ttlSeconds": 900, "grants": ["data"], "iceServers": []}))
        assert wait_until(lambda: stream_ids())
    finally:
        device.stop()


def test_the_camera_never_starts_just_because_the_device_did():
    from pihub import Program
    from pihub.video import TestPatternSource

    source = TestPatternSource()
    device = Device(id="birdbox01", transport=FakeTransport(), webrtc=True)
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(source)
    device.add_program(program)
    device.start()
    try:
        time.sleep(0.5)
        assert source.running is False
    finally:
        device.stop()


def test_a_video_output_with_no_webrtc_publishes_no_stream_id():
    from pihub import Program
    from pihub.video import TestPatternSource

    transport = FakeTransport()
    device = Device(id="birdbox01", transport=transport)  # a caller-supplied transport: no WebRTC
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(TestPatternSource())
    device.add_program(program)
    device.start()
    try:
        assert not [1 for raw in transport.sent if json.loads(raw)["messageType"] == "runtime"]
    finally:
        device.stop()


def test_importing_the_video_package_imports_neither_aiortc_nor_picamera2():
    code = "import sys, pihub.video; assert 'aiortc' not in sys.modules and 'picamera2' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).returncode == 0


def test_a_missing_camera_library_is_a_clear_error_not_a_crash(monkeypatch):
    from pihub.video import CameraUnavailable, PiCameraSource

    monkeypatch.setitem(sys.modules, "picamera2", None)  # makes `import picamera2` fail
    with pytest.raises(CameraUnavailable) as raised:
        PiCameraSource().start()
    assert "picamera2" in str(raised.value)


def test_video_sizes_must_be_even_and_rates_sane():
    from pihub.video import TestPatternSource

    for bad in ((641, 480), (0, 480), (640, -2)):
        with pytest.raises(ValueError):
            TestPatternSource(size=bad)
    with pytest.raises(ValueError):
        TestPatternSource(fps=0)


# -- the warning when the library is missing (2026-09-30: it used to be silent unless the program asked for webrtc=True) ---------------------------------


@pytest.fixture
def no_library(monkeypatch):
    import pihub.webrtc.peer_manager as peer_manager

    monkeypatch.setattr(peer_manager, "webrtc_available", lambda: False)
    monkeypatch.setattr("pihub.webrtc.webrtc_available", lambda: False)


def _warnings_about_the_library(caplog):
    return [r for r in caplog.records if r.levelname == "WARNING" and "aiortc" in r.getMessage()]


def test_a_device_that_wants_webrtc_by_default_says_why_when_the_library_is_missing(no_library, caplog):
    device = Device(id="birdbox01", transport=FakeTransport())
    with caplog.at_level("WARNING"):
        device._start_webrtc(built_here=True)  # what start() does for a device that built its own connection
    assert device._peers is None
    assert len(_warnings_about_the_library(caplog)) == 1


def test_a_device_with_a_caller_supplied_transport_stays_quiet_when_the_library_is_missing(no_library, caplog):
    """A test or simulation that passes its own transport never wanted WebRTC, so there is nothing to warn about."""
    device = Device(id="birdbox01", transport=FakeTransport())
    with caplog.at_level("WARNING"):
        device.start()
    try:
        assert not _warnings_about_the_library(caplog)
    finally:
        device.stop()


def test_webrtc_false_keeps_it_quiet(no_library, caplog):
    device = Device(id="birdbox01", transport=FakeTransport(), webrtc=False)
    with caplog.at_level("WARNING"):
        device.start()
    try:
        assert not _warnings_about_the_library(caplog)
    finally:
        device.stop()


def test_the_environment_switch_keeps_it_quiet(no_library, monkeypatch, caplog):
    monkeypatch.setenv("PIHUB_WEBRTC", "0")
    device = Device(id="birdbox01", transport=FakeTransport())
    with caplog.at_level("WARNING"):
        device._start_webrtc(built_here=True)
    assert not _warnings_about_the_library(caplog)


@pytest.mark.parametrize("machine", ["armv6l", "armv7l", "armv8l"])
def test_on_a_32_bit_pi_the_hint_is_the_apt_route(machine, monkeypatch):
    import pihub.device as device_module

    monkeypatch.setattr(device_module.platform, "machine", lambda: machine)
    hint = device_module._webrtc_install_hint()
    assert "sudo apt install python3-aiortc python3-av" in hint
    assert "--system-site-packages" in hint


@pytest.mark.parametrize("machine", ["x86_64", "aarch64", "AMD64"])
def test_elsewhere_the_hint_is_pip_and_a_broken_library(machine, monkeypatch):
    import pihub.device as device_module

    monkeypatch.setattr(device_module.platform, "machine", lambda: machine)
    hint = device_module._webrtc_install_hint()
    assert "pip install --upgrade aiortc" in hint
    assert "apt" not in hint
