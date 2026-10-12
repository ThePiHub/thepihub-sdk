import json
import re

import pytest

from pihub.protocol.envelope import CURRENT_PROTOCOL_VERSION, Envelope


def test_defaults_set_protocol_version_and_timestamp():
    envelope = Envelope(message_type="runtime", payload={"value": 1})

    assert envelope.protocol_version == CURRENT_PROTOCOL_VERSION
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", envelope.timestamp)


def test_payload_defaults_to_empty_dict():
    envelope = Envelope(message_type="notification")

    assert envelope.payload == {}


def test_to_dict_uses_wire_format_keys():
    envelope = Envelope(
        message_type="runtime",
        payload={"value": 22.5},
        protocol_version=1,
        timestamp="2026-09-14T18:00:00Z",
    )

    assert envelope.to_dict() == {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {"value": 22.5},
    }


def test_to_json_round_trips_through_json_loads():
    envelope = Envelope(
        message_type="runtime",
        payload={"value": 22.5},
        timestamp="2026-09-14T18:00:00Z",
    )

    decoded = json.loads(envelope.to_json())

    assert decoded == envelope.to_dict()


def test_from_dict_parses_valid_envelope():
    data = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {"value": 22.5},
    }

    envelope = Envelope.from_dict(data)

    assert envelope == Envelope(
        message_type="runtime",
        payload={"value": 22.5},
        protocol_version=1,
        timestamp="2026-09-14T18:00:00Z",
    )


def test_from_dict_ignores_unknown_top_level_fields():
    data = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
        "somethingFromTheFuture": True,
    }

    envelope = Envelope.from_dict(data)

    assert envelope.payload == {}


def test_from_json_parses_json_string():
    raw = json.dumps(
        {
            "protocolVersion": 1,
            "messageType": "notification",
            "timestamp": "2026-09-14T18:00:00Z",
            "payload": {"severity": "info", "message": "hello"},
        }
    )

    envelope = Envelope.from_json(raw)

    assert envelope.message_type == "notification"
    assert envelope.payload == {"severity": "info", "message": "hello"}


def test_from_json_round_trips_with_to_json():
    original = Envelope(
        message_type="command",
        payload={"binding": {"deviceId": "birdbox01", "programId": "birdbox", "widgetId": "snapshot"}, "action": "press"},
        timestamp="2026-09-14T18:00:00Z",
    )

    assert Envelope.from_json(original.to_json()) == original


def test_from_json_raises_for_malformed_json():
    with pytest.raises(json.JSONDecodeError):
        Envelope.from_json("{not valid json")


def test_from_json_raises_for_json_missing_required_field():
    raw = json.dumps(
        {
            "protocolVersion": 1,
            "messageType": "runtime",
            "payload": {},
        }
    )

    with pytest.raises(ValueError, match="timestamp"):
        Envelope.from_json(raw)


def test_from_json_raises_for_json_that_is_not_an_object():
    with pytest.raises(ValueError, match="object"):
        Envelope.from_json("[1, 2, 3]")


def test_round_trip_to_dict_from_dict():
    original = Envelope(
        message_type="command",
        payload={"binding": {"programId": "birdbox", "widgetId": "snapshot"}, "action": "press"},
        timestamp="2026-09-14T18:00:00Z",
    )

    assert Envelope.from_dict(original.to_dict()) == original


def test_from_dict_rejects_non_dict():
    with pytest.raises(ValueError, match="object"):
        Envelope.from_dict("not a dict")


@pytest.mark.parametrize(
    "missing_field",
    ["protocolVersion", "messageType", "timestamp", "payload"],
)
def test_from_dict_requires_each_field(missing_field):
    data = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }
    del data[missing_field]

    with pytest.raises(ValueError, match=missing_field):
        Envelope.from_dict(data)


def test_from_dict_rejects_non_integer_protocol_version():
    data = {
        "protocolVersion": "1",
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }

    with pytest.raises(ValueError, match="protocolVersion"):
        Envelope.from_dict(data)


def test_from_dict_rejects_boolean_protocol_version():
    # bool is a subclass of int in Python; must not be accepted as a version.
    data = {
        "protocolVersion": True,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }

    with pytest.raises(ValueError, match="protocolVersion"):
        Envelope.from_dict(data)


def test_from_dict_rejects_non_string_message_type():
    data = {
        "protocolVersion": 1,
        "messageType": 123,
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }

    with pytest.raises(ValueError, match="messageType"):
        Envelope.from_dict(data)


def test_from_dict_rejects_non_string_timestamp():
    data = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": 12345,
        "payload": {},
    }

    with pytest.raises(ValueError, match="timestamp"):
        Envelope.from_dict(data)


def test_from_dict_rejects_non_object_payload():
    data = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": "not an object",
    }

    with pytest.raises(ValueError, match="payload"):
        Envelope.from_dict(data)


def test_accepts_future_protocol_version_without_error():
    data = {
        "protocolVersion": 2,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }

    envelope = Envelope.from_dict(data)

    assert envelope.protocol_version == 2


def test_accepts_unknown_message_type_without_error():
    data = {
        "protocolVersion": 1,
        "messageType": "futureMessageType",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {},
    }

    envelope = Envelope.from_dict(data)

    assert envelope.message_type == "futureMessageType"
