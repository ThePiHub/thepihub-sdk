import pytest

from pihub.protocol.bindings import Binding


def test_to_dict_includes_device_id():
    binding = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")

    assert binding.to_dict() == {
        "deviceId": "greenhouse01",
        "programId": "weather",
        "widgetId": "temperature",
    }


def test_from_dict_with_device_id():
    data = {"deviceId": "greenhouse01", "programId": "weather", "widgetId": "temperature"}

    binding = Binding.from_dict(data)

    assert binding == Binding(
        device_id="greenhouse01", program_id="weather", widget_id="temperature"
    )


def test_from_dict_missing_device_id_raises():
    with pytest.raises(ValueError, match="deviceId"):
        Binding.from_dict({"programId": "weather", "widgetId": "temperature"})


def test_from_dict_missing_program_id_raises():
    with pytest.raises(ValueError, match="programId"):
        Binding.from_dict({"deviceId": "greenhouse01", "widgetId": "temperature"})


def test_from_dict_missing_widget_id_raises():
    with pytest.raises(ValueError, match="widgetId"):
        Binding.from_dict({"deviceId": "greenhouse01", "programId": "weather"})


def test_from_dict_missing_all_raises_with_all_names():
    with pytest.raises(ValueError, match="deviceId, programId, widgetId"):
        Binding.from_dict({})


def test_round_trip_to_dict_from_dict():
    original = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")

    assert Binding.from_dict(original.to_dict()) == original


def test_canonical_id():
    binding = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")

    assert binding.canonical_id == "greenhouse01.weather.temperature"


def test_from_canonical_id():
    binding = Binding.from_canonical_id("greenhouse01.weather.temperature")

    assert binding == Binding(
        device_id="greenhouse01", program_id="weather", widget_id="temperature"
    )


def test_from_canonical_id_round_trips_with_canonical_id_property():
    original = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")

    assert Binding.from_canonical_id(original.canonical_id) == original


@pytest.mark.parametrize(
    "canonical_id",
    ["", "onlyDevice", "device.program", "device.program.widget.extra"],
)
def test_from_canonical_id_rejects_wrong_component_count(canonical_id):
    with pytest.raises(ValueError, match="3"):
        Binding.from_canonical_id(canonical_id)
