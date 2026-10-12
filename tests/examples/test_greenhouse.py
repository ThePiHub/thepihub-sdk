from typing import Any, Dict, List

from pihub import Device
from pihub.protocol.bindings import Binding
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import CommandMessage
from examples import greenhouse


def _payloads(transport, message_type: str) -> List[Dict[str, Any]]:
    return [
        Envelope.from_json(raw).payload
        for raw in transport.sent
        if Envelope.from_json(raw).message_type == message_type
    ]


def test_runs_without_exceptions(transport):
    greenhouse.main(transport=transport)  # should not raise


def test_returns_the_device(transport):
    device = greenhouse.main(transport=transport)
    assert isinstance(device, Device)
    assert device.id == "example-device"


def test_registration_declares_sensors_and_command(transport):
    greenhouse.main(transport=transport)
    [registration] = _payloads(transport, "registration")
    widgets = {w["widgetId"]: w["type"] for w in registration["programs"][0]["widgets"]}
    assert widgets == {
        "temperature": "Gauge",
        "humidity": "Gauge",
        "waterPlants": "Button",
    }


def test_sends_sensor_readings(transport):
    greenhouse.main(transport=transport)
    runtime_payloads = _payloads(transport, "runtime")
    values = {p["binding"]["widgetId"]: p["value"] for p in runtime_payloads}
    assert values == {"temperature": 24.0, "humidity": 38.0}


def test_sends_a_warning_notification(transport):
    greenhouse.main(transport=transport)
    notifications = _payloads(transport, "notification")
    warning = next(n for n in notifications if n["severity"] == "warning")
    assert warning["message"] == "Soil moisture low"


def test_water_button_press_triggers_info_notification(transport):
    device = greenhouse.main(transport=transport)
    binding = Binding(device_id="example-device", program_id="greenhouse", widget_id="waterPlants")
    device.handle_incoming(CommandMessage(binding=binding, action="press").to_json())
    notifications = _payloads(transport, "notification")
    info_messages = [n["message"] for n in notifications if n["severity"] == "info"]
    assert "Plants watered" in info_messages


def test_message_order(transport):
    greenhouse.main(transport=transport)
    types = [Envelope.from_json(raw).message_type for raw in transport.sent]
    assert types == [
        "registration",
        "runtime",
        "runtime",
        "notification",  # warning: soil moisture low
    ]
