import pytest

from pihub.protocol.bindings import Binding
from pihub.runtime.store import RuntimeStore
from pihub.runtime.value import RuntimeValue


@pytest.fixture
def store() -> RuntimeStore:
    return RuntimeStore()


TEMPERATURE = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")
TEMPERATURE_KEY = "greenhouse01.weather.temperature"


# --- update() -------------------------------------------------------------


def test_update_with_binding_stores_single_value(store):
    entry = store.update(TEMPERATURE, 22.5)

    assert entry.value == 22.5
    assert store.get(TEMPERATURE).value == 22.5


def test_update_with_canonical_string_key(store):
    store.update(TEMPERATURE_KEY, 22.5)

    assert store.get(TEMPERATURE_KEY).value == 22.5


def test_update_with_multi_values(store):
    system = Binding(device_id="server01", program_id="health", widget_id="system")

    store.update(system, values={"cpu": 12, "memory": 45, "disk": 78})

    entry = store.get(system)
    assert entry.value is None
    assert entry.values == {"cpu": 12, "memory": 45, "disk": 78}


def test_update_returns_the_stored_runtime_value(store):
    entry = store.update(TEMPERATURE, 22.5)
    assert isinstance(entry, RuntimeValue)


def test_update_overwrites_previous_value_for_same_key(store):
    store.update(TEMPERATURE, 20.0)
    store.update(TEMPERATURE, 22.5)

    assert store.get(TEMPERATURE).value == 22.5


def test_update_with_explicit_timestamp(store):
    store.update(TEMPERATURE, 22.5, timestamp=1720000000)
    assert store.get(TEMPERATURE).timestamp == 1720000000


def test_update_requires_exactly_one_of_value_or_values(store):
    with pytest.raises(ValueError):
        store.update(TEMPERATURE)

    with pytest.raises(ValueError):
        store.update(TEMPERATURE, 1, values={"a": 1})


# --- get() -----------------------------------------------------------------


def test_get_returns_none_for_missing_key(store):
    assert store.get(TEMPERATURE) is None
    assert store.get("no.such.key") is None


def test_get_accepts_binding_or_string_interchangeably(store):
    store.update(TEMPERATURE, 22.5)  # written via Binding

    assert store.get(TEMPERATURE_KEY).value == 22.5  # read via string


def test_get_via_binding_after_update_via_string(store):
    store.update(TEMPERATURE_KEY, 22.5)  # written via string

    assert store.get(TEMPERATURE).value == 22.5  # read via Binding


# --- remove() ----------------------------------------------------------------


def test_remove_deletes_existing_entry(store):
    store.update(TEMPERATURE, 22.5)
    store.remove(TEMPERATURE)
    assert store.get(TEMPERATURE) is None


def test_remove_missing_key_is_a_noop(store):
    store.remove(TEMPERATURE)  # should not raise


def test_remove_only_affects_the_given_key(store):
    humidity = Binding(device_id="greenhouse01", program_id="weather", widget_id="humidity")
    store.update(TEMPERATURE, 22.5)
    store.update(humidity, 64)

    store.remove(TEMPERATURE)

    assert store.get(TEMPERATURE) is None
    assert store.get(humidity).value == 64


# --- clear() -------------------------------------------------------------


def test_clear_removes_everything(store):
    store.update(TEMPERATURE, 22.5)
    store.update(Binding(device_id="server01", program_id="health", widget_id="system"), values={"cpu": 1})

    store.clear()

    assert store.get(TEMPERATURE) is None
    assert store._values == {}


def test_clear_on_empty_store_is_a_noop(store):
    store.clear()  # should not raise


# --- clear_device() ---------------------------------------------------------


def test_clear_device_removes_only_that_devices_entries(store):
    humidity = Binding(device_id="greenhouse01", program_id="weather", widget_id="humidity")
    other_device = Binding(device_id="server01", program_id="health", widget_id="system")
    store.update(TEMPERATURE, 22.5)
    store.update(humidity, 64)
    store.update(other_device, values={"cpu": 1})

    store.clear_device("greenhouse01")

    assert store.get(TEMPERATURE) is None
    assert store.get(humidity) is None
    assert store.get(other_device) is not None


def test_clear_device_does_not_match_a_device_id_prefix(store):
    """Clearing "birdbox01" must not clear "birdbox010"'s entries."""
    victim = Binding(device_id="birdbox010", program_id="birdbox", widget_id="camera")
    store.update(victim, "online")

    store.clear_device("birdbox01")

    assert store.get(victim) is not None


def test_clear_device_with_no_matching_entries_is_a_noop(store):
    store.update(TEMPERATURE, 22.5)
    store.clear_device("nonexistent")
    assert store.get(TEMPERATURE) is not None


# --- integration: matches the spec's Store Example --------------------------


def test_matches_state_management_spec_store_example(store):
    """Mirrors the runtime store's documented example."""
    store.update(TEMPERATURE, 22.5, timestamp=1720000000)
    humidity = Binding(device_id="greenhouse01", program_id="weather", widget_id="humidity")
    store.update(humidity, 64, timestamp=1720000000)

    assert store.get(TEMPERATURE_KEY).to_dict() == {"value": 22.5, "timestamp": 1720000000}
    assert store.get("greenhouse01.weather.humidity").to_dict() == {"value": 64, "timestamp": 1720000000}


def test_disconnect_scenario_uses_clear_device(store):
    """Mirrors "Runtime Store API" > Clear Device Values: "Useful when a
    device disconnects."."""
    camera = Binding(device_id="birdbox01", program_id="birdbox", widget_id="camera")
    store.update(camera, "online")
    assert store.get(camera).value == "online"

    store.clear_device("birdbox01")  # device disconnected

    assert store.get(camera) is None
