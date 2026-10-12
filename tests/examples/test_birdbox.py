from typing import Any, Dict, List

import pytest

pytest.importorskip("av")  # the Camera component's test pattern needs PyAV

from pihub import Device  # noqa: E402
from pihub.protocol.envelope import Envelope  # noqa: E402
from pihub.protocol.messages import LifecycleMessage  # noqa: E402
from examples import birdbox  # noqa: E402


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # The Camera's default snapshot and recording folders are under the home directory.
    monkeypatch.setenv("HOME", str(tmp_path))


def _payloads(transport, message_type: str) -> List[Dict[str, Any]]:
    return [
        Envelope.from_json(raw).payload
        for raw in transport.sent
        if Envelope.from_json(raw).message_type == message_type
    ]


def test_runs_without_exceptions(transport):
    birdbox.main(transport=transport)  # should not raise


def test_returns_the_device(transport):
    device = birdbox.main(transport=transport)
    assert isinstance(device, Device)
    assert device.id == "example-device"


def test_sends_registration_first(transport):
    birdbox.main(transport=transport)
    assert Envelope.from_json(transport.sent[0]).message_type == "registration"


def test_registration_declares_a_camera_and_a_gauge(transport):
    birdbox.main(transport=transport)
    [registration] = _payloads(transport, "registration")
    widgets = {w["widgetId"]: w["type"] for w in registration["programs"][0]["widgets"]}
    assert widgets == {"camera": "Camera", "temperature": "Gauge"}


def test_the_camera_offers_snapshot_and_record(transport):
    birdbox.main(transport=transport)
    [registration] = _payloads(transport, "registration")
    camera = next(w for w in registration["programs"][0]["widgets"] if w["widgetId"] == "camera")
    assert camera["defaultProperties"] == {"snapshot": True, "record": True}


def test_sends_the_temperature(transport):
    birdbox.main(transport=transport)
    values = {p["binding"]["widgetId"]: p.get("value") for p in _payloads(transport, "runtime")}
    assert values["temperature"] == 22.5


def test_viewers_arriving_and_leaving_send_notifications(transport):
    device = birdbox.main(transport=transport)
    device.handle_incoming(LifecycleMessage.first_user_connected().to_json())
    device.handle_incoming(LifecycleMessage.last_user_disconnected().to_json())
    messages = [p["message"] for p in _payloads(transport, "notification")]
    assert messages == ["Viewer connected", "Last viewer disconnected"]


# -- a Pi 5: a camera, but no hardware encoder ---------------------------------------------------------------------------


def _camera_widget(transport):
    [registration] = _payloads(transport, "registration")
    return next(w for w in registration["programs"][0]["widgets"] if w["widgetId"] == "camera")


def test_a_pi_with_a_camera_but_no_hardware_encoder_and_no_libx264_streams_its_camera_without_a_record_button(transport, monkeypatch):
    """The last resort: the camera's raw frames, which aiortc encodes. It streams and takes snapshots, but cannot be recorded."""
    from pihub.video import HardwareCameraSource, PiCameraSource, SoftwareEncodedCameraSource

    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(PiCameraSource, "available", staticmethod(lambda: True))

    chosen = []
    real_choice = birdbox.best_camera_source
    monkeypatch.setattr(birdbox, "best_camera_source", lambda: chosen.append(real_choice()) or chosen[-1])

    birdbox.main(transport=transport)

    assert type(chosen[0]) is PiCameraSource  # its camera, encoded on the CPU, not the test pattern
    assert _camera_widget(transport)["defaultProperties"] == {"snapshot": True}  # no "record"


def test_a_pi_5_with_libx264_records_too(transport, monkeypatch):
    """A Pi with no hardware encoder now gets the software-ENCODED source, which hands on H.264 packets like the hardware one: so Record is offered."""
    from pihub.video import HardwareCameraSource, SoftwareEncodedCameraSource

    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: False))
    monkeypatch.setattr(SoftwareEncodedCameraSource, "available", staticmethod(lambda: True))
    chosen = []
    real_choice = birdbox.best_camera_source
    monkeypatch.setattr(birdbox, "best_camera_source", lambda: chosen.append(real_choice()) or chosen[-1])

    birdbox.main(transport=transport)

    assert type(chosen[0]) is SoftwareEncodedCameraSource
    assert _camera_widget(transport)["defaultProperties"] == {"snapshot": True, "record": True}


def test_a_pi_with_the_hardware_encoder_still_offers_record(transport, monkeypatch):
    from pihub.video import HardwareCameraSource

    monkeypatch.setattr(HardwareCameraSource, "available", staticmethod(lambda: True))

    birdbox.main(transport=transport)

    assert _camera_widget(transport)["defaultProperties"] == {"snapshot": True, "record": True}

