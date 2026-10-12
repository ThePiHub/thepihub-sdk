import time

import pytest

from pihub.runtime.value import RuntimeValue


def test_requires_exactly_one_of_value_or_values():
    with pytest.raises(ValueError):
        RuntimeValue()


def test_rejects_both_value_and_values():
    with pytest.raises(ValueError):
        RuntimeValue(value=1, values={"a": 1})


def test_single_value_is_stored():
    entry = RuntimeValue(value=22.5)
    assert entry.value == 22.5
    assert entry.values is None


def test_multi_values_are_stored():
    entry = RuntimeValue(values={"cpu": 12, "memory": 45})
    assert entry.value is None
    assert entry.values == {"cpu": 12, "memory": 45}


def test_timestamp_defaults_to_now():
    before = time.time()
    entry = RuntimeValue(value=1)
    after = time.time()

    assert before <= entry.timestamp <= after


def test_explicit_timestamp_is_preserved():
    entry = RuntimeValue(value=1, timestamp=1720000000)
    assert entry.timestamp == 1720000000


# --- to_dict(), matching the spec's Store Example shape ---------------------


def test_to_dict_for_single_value():
    entry = RuntimeValue(value=22.5, timestamp=1720000000)
    assert entry.to_dict() == {"value": 22.5, "timestamp": 1720000000}


def test_to_dict_for_multi_values():
    entry = RuntimeValue(values={"cpu": 12, "memory": 45, "disk": 78}, timestamp=1720000000)
    assert entry.to_dict() == {"values": {"cpu": 12, "memory": 45, "disk": 78}, "timestamp": 1720000000}


def test_to_dict_omits_values_key_for_single_value():
    entry = RuntimeValue(value=22.5, timestamp=1720000000)
    assert "values" not in entry.to_dict()


def test_to_dict_omits_value_key_for_multi_values():
    entry = RuntimeValue(values={"cpu": 12}, timestamp=1720000000)
    assert "value" not in entry.to_dict()
