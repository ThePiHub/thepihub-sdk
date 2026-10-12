from typing import Any, Dict, List

from pihub import Device
from pihub.protocol.envelope import Envelope
from examples import weather_station


def _payloads(transport, message_type: str) -> List[Dict[str, Any]]:
    return [
        Envelope.from_json(raw).payload
        for raw in transport.sent
        if Envelope.from_json(raw).message_type == message_type
    ]


def test_runs_without_exceptions(transport):
    weather_station.main(transport=transport, cycles=2, interval=0)  # should not raise


def test_returns_the_device(transport):
    device = weather_station.main(transport=transport, cycles=1, interval=0)
    assert isinstance(device, Device)
    assert device.id == "example-device"


def test_registration_declares_three_gauges_and_a_chart(transport):
    weather_station.main(transport=transport, cycles=1, interval=0)
    [registration] = _payloads(transport, "registration")
    widgets = {w["widgetId"]: w["type"] for w in registration["programs"][0]["widgets"]}
    assert widgets == {
        "temperature": "Gauge",
        "humidity": "Gauge",
        "pressure": "Gauge",
        "conditions": "Chart",
    }


def test_each_cycle_sends_three_single_value_updates_and_one_multi_value_update(transport):
    weather_station.main(transport=transport, cycles=3, interval=0)
    runtime_payloads = _payloads(transport, "runtime")

    single_value_updates = [p for p in runtime_payloads if "value" in p]
    multi_value_updates = [p for p in runtime_payloads if "values" in p]

    assert len(single_value_updates) == 3 * 3  # 3 gauges x 3 cycles
    assert len(multi_value_updates) == 3  # 1 conditions update x 3 cycles


def test_single_value_readings_are_within_realistic_ranges(transport):
    weather_station.main(transport=transport, cycles=5, interval=0)
    runtime_payloads = _payloads(transport, "runtime")

    for payload in runtime_payloads:
        widget_id = payload["binding"]["widgetId"]
        if widget_id == "temperature":
            assert 15.0 <= payload["value"] <= 30.0
        elif widget_id == "humidity":
            assert 30.0 <= payload["value"] <= 90.0
        elif widget_id == "pressure":
            assert 980.0 <= payload["value"] <= 1050.0


def test_multi_value_update_bundles_all_three_readings(transport):
    weather_station.main(transport=transport, cycles=1, interval=0)
    [conditions_payload] = _payloads(transport, "runtime")[-1:]
    assert set(conditions_payload["values"].keys()) == {"temperature", "humidity", "pressure"}


def test_periodic_updates_with_nonzero_interval(transport):
    # Exercises the sleep-between-cycles branch, with a negligible delay.
    device = weather_station.main(transport=transport, cycles=2, interval=0.001)
    assert isinstance(device, Device)
    runtime_payloads = _payloads(transport, "runtime")
    assert len(runtime_payloads) == 2 * 4  # 3 gauges + 1 chart, per cycle
