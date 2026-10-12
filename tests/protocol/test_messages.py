import pytest

from pihub.protocol.bindings import Binding
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import (
    CAPABILITY_COMMAND,
    CAPABILITY_DISPLAY,
    CommandMessage,
    KNOWN_MESSAGE_TYPES,
    LIFECYCLE_DASHBOARD_CLOSED,
    LIFECYCLE_DASHBOARD_OPENED,
    LIFECYCLE_FIRST_USER_CONNECTED,
    LIFECYCLE_LAST_USER_DISCONNECTED,
    LIFECYCLE_VIEWER_COUNT_CHANGED,
    LifecycleMessage,
    MESSAGE_TYPE_COMMAND,
    MESSAGE_TYPE_LIFECYCLE,
    MESSAGE_TYPE_NOTIFICATION,
    MESSAGE_TYPE_REGISTRATION,
    MESSAGE_TYPE_RUNTIME,
    NotificationMessage,
    ProgramRegistration,
    RegistrationMessage,
    RuntimeMessage,
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    WidgetRegistration,
    is_known_message_type,
    parse_message,
)


BINDING = Binding(device_id="greenhouse01", program_id="weather", widget_id="temperature")


# --- RuntimeMessage ---------------------------------------------------


def test_runtime_message_single_value_payload():
    message = RuntimeMessage(binding=BINDING, value=22.5)

    assert message.to_payload() == {"binding": BINDING.to_dict(), "value": 22.5}


def test_runtime_message_multi_value_payload():
    message = RuntimeMessage(binding=BINDING, values={"cpu": 10, "memory": 42, "disk": 77})

    assert message.to_payload() == {
        "binding": BINDING.to_dict(),
        "values": {"cpu": 10, "memory": 42, "disk": 77},
    }


def test_runtime_message_requires_exactly_one_of_value_or_values():
    with pytest.raises(ValueError, match="exactly one"):
        RuntimeMessage(binding=BINDING)

    with pytest.raises(ValueError, match="exactly one"):
        RuntimeMessage(binding=BINDING, value=1, values={"a": 1})


def test_runtime_message_from_payload():
    payload = {"binding": BINDING.to_dict(), "value": 22.5}

    message = RuntimeMessage.from_payload(payload)

    assert message == RuntimeMessage(binding=BINDING, value=22.5)


def test_runtime_message_from_payload_missing_binding_raises():
    with pytest.raises(ValueError, match="binding"):
        RuntimeMessage.from_payload({"value": 1})


def test_runtime_message_from_payload_requires_exactly_one_value_field():
    with pytest.raises(ValueError, match="exactly one"):
        RuntimeMessage.from_payload({"binding": BINDING.to_dict()})

    with pytest.raises(ValueError, match="exactly one"):
        RuntimeMessage.from_payload(
            {"binding": BINDING.to_dict(), "value": 1, "values": {"a": 1}}
        )


def test_runtime_message_from_payload_with_values():
    payload = {"binding": BINDING.to_dict(), "values": {"cpu": 10, "memory": 42}}

    message = RuntimeMessage.from_payload(payload)

    assert message == RuntimeMessage(binding=BINDING, values={"cpu": 10, "memory": 42})


def test_runtime_message_from_payload_propagates_invalid_binding_error():
    with pytest.raises(ValueError, match="widgetId"):
        RuntimeMessage.from_payload({"binding": {"programId": "weather"}, "value": 1})


def test_runtime_message_to_envelope_uses_runtime_type():
    envelope = RuntimeMessage(binding=BINDING, value=22.5).to_envelope()

    assert envelope.message_type == MESSAGE_TYPE_RUNTIME
    assert envelope.payload["value"] == 22.5


def test_runtime_message_round_trips_through_envelope():
    original = RuntimeMessage(binding=BINDING, value=22.5)

    restored = RuntimeMessage.from_envelope(original.to_envelope())

    assert restored == original


def test_runtime_message_round_trips_through_json():
    original = RuntimeMessage(binding=BINDING, values={"cpu": 10})

    restored = RuntimeMessage.from_json(original.to_json())

    assert restored == original


# --- CommandMessage -----------------------------------------------------


def test_command_message_action_payload():
    binding = Binding(device_id="birdbox01", program_id="birdbox", widget_id="snapshot")
    message = CommandMessage(binding=binding, action="press")

    assert message.to_payload() == {"binding": binding.to_dict(), "action": "press"}


def test_command_message_values_payload():
    binding = Binding(device_id="robot01", program_id="control", widget_id="joystick")
    message = CommandMessage(binding=binding, values={"x": 50, "y": -20})

    assert message.to_payload() == {
        "binding": binding.to_dict(),
        "values": {"x": 50, "y": -20},
    }


def test_command_message_requires_exactly_one_of_action_or_values():
    with pytest.raises(ValueError, match="exactly one"):
        CommandMessage(binding=BINDING)

    with pytest.raises(ValueError, match="exactly one"):
        CommandMessage(binding=BINDING, action="press", values={"x": 1})


def test_command_message_from_payload_with_action():
    payload = {"binding": BINDING.to_dict(), "action": "press"}

    message = CommandMessage.from_payload(payload)

    assert message == CommandMessage(binding=BINDING, action="press")


def test_command_message_from_payload_with_values():
    payload = {"binding": BINDING.to_dict(), "values": {"x": 50, "y": -20}}

    message = CommandMessage.from_payload(payload)

    assert message == CommandMessage(binding=BINDING, values={"x": 50, "y": -20})


def test_command_message_from_payload_missing_binding_raises():
    with pytest.raises(ValueError, match="binding"):
        CommandMessage.from_payload({"action": "press"})


def test_command_message_from_payload_requires_exactly_one_field():
    with pytest.raises(ValueError, match="exactly one"):
        CommandMessage.from_payload({"binding": BINDING.to_dict()})

    with pytest.raises(ValueError, match="exactly one"):
        CommandMessage.from_payload(
            {"binding": BINDING.to_dict(), "action": "press", "values": {"x": 1}}
        )


def test_command_message_from_payload_propagates_invalid_binding_error():
    with pytest.raises(ValueError, match="widgetId"):
        CommandMessage.from_payload({"binding": {"programId": "birdbox"}, "action": "press"})


def test_command_message_round_trips_through_envelope():
    original = CommandMessage(binding=BINDING, action="press")

    restored = CommandMessage.from_envelope(original.to_envelope())

    assert restored == original
    assert restored.to_envelope().message_type == MESSAGE_TYPE_COMMAND


# --- LifecycleMessage -----------------------------------------------------


def test_lifecycle_message_first_user_connected():
    message = LifecycleMessage.first_user_connected()

    assert message.to_payload() == {"event": LIFECYCLE_FIRST_USER_CONNECTED}


def test_lifecycle_message_last_user_disconnected():
    message = LifecycleMessage.last_user_disconnected()

    assert message.to_payload() == {"event": LIFECYCLE_LAST_USER_DISCONNECTED}


def test_lifecycle_message_viewer_count_changed():
    message = LifecycleMessage.viewer_count_changed(3)

    assert message.to_payload() == {
        "event": LIFECYCLE_VIEWER_COUNT_CHANGED,
        "viewerCount": 3,
    }


def test_lifecycle_message_dashboard_opened():
    message = LifecycleMessage.dashboard_opened("birdbox")

    assert message.to_payload() == {
        "event": LIFECYCLE_DASHBOARD_OPENED,
        "dashboardId": "birdbox",
    }


def test_lifecycle_message_dashboard_closed():
    message = LifecycleMessage.dashboard_closed("birdbox")

    assert message.to_payload() == {
        "event": LIFECYCLE_DASHBOARD_CLOSED,
        "dashboardId": "birdbox",
    }


def test_lifecycle_message_from_payload_separates_event_and_data():
    payload = {"event": LIFECYCLE_VIEWER_COUNT_CHANGED, "viewerCount": 3}

    message = LifecycleMessage.from_payload(payload)

    assert message == LifecycleMessage(
        event=LIFECYCLE_VIEWER_COUNT_CHANGED, data={"viewerCount": 3}
    )


def test_lifecycle_message_from_payload_missing_event_raises():
    with pytest.raises(ValueError, match="event"):
        LifecycleMessage.from_payload({})


def test_lifecycle_message_round_trips_through_envelope():
    original = LifecycleMessage.dashboard_opened("birdbox")

    restored = LifecycleMessage.from_envelope(original.to_envelope())

    assert restored == original
    assert restored.to_envelope().message_type == MESSAGE_TYPE_LIFECYCLE


# --- NotificationMessage --------------------------------------------------


def test_notification_message_info_factory():
    message = NotificationMessage.info("birdbox01", "birdbox", "Motion detection started")

    assert message == NotificationMessage(
        device_id="birdbox01",
        program_id="birdbox",
        severity=SEVERITY_INFO,
        message="Motion detection started",
    )


def test_notification_message_warning_factory():
    message = NotificationMessage.warning("birdbox01", "birdbox", "Storage 90% full")

    assert message.severity == SEVERITY_WARNING


def test_notification_message_error_factory():
    message = NotificationMessage.error("birdbox01", "birdbox", "Camera disconnected")

    assert message.severity == SEVERITY_ERROR


def test_notification_message_to_payload():
    message = NotificationMessage(
        device_id="birdbox01", program_id="birdbox", severity=SEVERITY_INFO, message="hello"
    )

    assert message.to_payload() == {
        "deviceId": "birdbox01",
        "programId": "birdbox",
        "severity": "info",
        "message": "hello",
    }


def test_notification_message_from_payload_missing_fields_raises():
    base = {"deviceId": "birdbox01", "programId": "birdbox", "severity": "info", "message": "hello"}

    for missing_field in ("deviceId", "programId", "severity", "message"):
        payload = {k: v for k, v in base.items() if k != missing_field}
        with pytest.raises(ValueError, match=missing_field):
            NotificationMessage.from_payload(payload)


def test_notification_message_round_trips_through_envelope():
    original = NotificationMessage.error("birdbox01", "birdbox", "Camera disconnected")

    restored = NotificationMessage.from_envelope(original.to_envelope())

    assert restored == original
    assert restored.to_envelope().message_type == MESSAGE_TYPE_NOTIFICATION


# --- Registration (WidgetRegistration / ProgramRegistration / RegistrationMessage) --


def test_widget_registration_to_dict():
    widget = WidgetRegistration(
        widget_id="camera", type="Video", capabilities=[CAPABILITY_DISPLAY]
    )

    assert widget.to_dict() == {
        "widgetId": "camera",
        "type": "Video",
        "capabilities": ["display"],
    }


def test_widget_registration_capabilities_default_to_empty_list():
    widget = WidgetRegistration(widget_id="camera", type="Video")

    assert widget.to_dict()["capabilities"] == []


def test_widget_registration_from_dict():
    data = {"widgetId": "snapshot", "type": "Button", "capabilities": [CAPABILITY_COMMAND]}

    widget = WidgetRegistration.from_dict(data)

    assert widget == WidgetRegistration(
        widget_id="snapshot", type="Button", capabilities=["command"]
    )


def test_widget_registration_from_dict_missing_fields_raises():
    with pytest.raises(ValueError, match="widgetId"):
        WidgetRegistration.from_dict({"type": "Button"})

    with pytest.raises(ValueError, match="type"):
        WidgetRegistration.from_dict({"widgetId": "snapshot"})


# --- WidgetRegistration property metadata --------------------------------


def test_widget_registration_property_schema_and_defaults_default_to_empty():
    widget = WidgetRegistration(widget_id="camera", type="Video")

    assert widget.property_schema == []
    assert widget.default_properties == {}


def test_widget_registration_to_dict_omits_property_metadata_when_empty():
    """Backward compatibility: a widget with no properties produces the
    exact same wire shape as before this feature existed."""
    widget = WidgetRegistration(widget_id="camera", type="Video", capabilities=[CAPABILITY_DISPLAY])

    assert widget.to_dict() == {
        "widgetId": "camera",
        "type": "Video",
        "capabilities": ["display"],
    }


def test_widget_registration_to_dict_includes_property_metadata_when_present():
    widget = WidgetRegistration(
        widget_id="temperature",
        type="Gauge",
        capabilities=[CAPABILITY_DISPLAY],
        property_schema=[
            {"name": "title", "label": "Title", "control": "text"},
            {"name": "units", "label": "Units", "control": "text"},
        ],
        default_properties={"title": "Temperature", "units": "°C"},
    )

    assert widget.to_dict() == {
        "widgetId": "temperature",
        "type": "Gauge",
        "capabilities": ["display"],
        "propertySchema": [
            {"name": "title", "label": "Title", "control": "text"},
            {"name": "units", "label": "Units", "control": "text"},
        ],
        "defaultProperties": {"title": "Temperature", "units": "°C"},
    }


def test_widget_registration_from_dict_reads_property_metadata():
    data = {
        "widgetId": "temperature",
        "type": "Gauge",
        "capabilities": [CAPABILITY_DISPLAY],
        "propertySchema": [{"name": "title", "label": "Title", "control": "text"}],
        "defaultProperties": {"title": "Temperature"},
    }

    widget = WidgetRegistration.from_dict(data)

    assert widget.property_schema == [{"name": "title", "label": "Title", "control": "text"}]
    assert widget.default_properties == {"title": "Temperature"}


def test_widget_registration_from_dict_without_property_metadata_defaults_to_empty():
    """Backward compatibility: a registration from before this feature
    existed (or from an older SDK) still parses."""
    data = {"widgetId": "camera", "type": "Video", "capabilities": [CAPABILITY_DISPLAY]}

    widget = WidgetRegistration.from_dict(data)

    assert widget.property_schema == []
    assert widget.default_properties == {}


def test_widget_registration_property_metadata_round_trips_through_dict():
    original = WidgetRegistration(
        widget_id="temperature",
        type="Gauge",
        capabilities=[CAPABILITY_DISPLAY],
        property_schema=[{"name": "title", "label": "Title", "control": "text"}],
        default_properties={"title": "Temperature"},
    )

    restored = WidgetRegistration.from_dict(original.to_dict())

    assert restored == original


def test_program_registration_to_dict():
    program = ProgramRegistration(
        program_id="birdbox",
        name="Bird Box",
        widgets=[
            WidgetRegistration(widget_id="camera", type="Video", capabilities=["display"]),
            WidgetRegistration(widget_id="snapshot", type="Button", capabilities=["command"]),
        ],
    )

    assert program.to_dict() == {
        "programId": "birdbox",
        "name": "Bird Box",
        "widgets": [
            {"widgetId": "camera", "type": "Video", "capabilities": ["display"]},
            {"widgetId": "snapshot", "type": "Button", "capabilities": ["command"]},
        ],
    }


def test_program_registration_from_dict_missing_fields_raises():
    with pytest.raises(ValueError, match="programId"):
        ProgramRegistration.from_dict({"name": "Bird Box"})

    with pytest.raises(ValueError, match="name"):
        ProgramRegistration.from_dict({"programId": "birdbox"})


def test_program_registration_widgets_default_to_empty_list():
    program = ProgramRegistration(program_id="birdbox", name="Bird Box")

    assert program.to_dict()["widgets"] == []


def _sample_registration_message() -> RegistrationMessage:
    return RegistrationMessage(
        device_id="birdbox01",
        programs=[
            ProgramRegistration(
                program_id="birdbox",
                name="Bird Box",
                widgets=[
                    WidgetRegistration(
                        widget_id="camera", type="Video", capabilities=["display"]
                    ),
                    WidgetRegistration(
                        widget_id="snapshot", type="Button", capabilities=["command"]
                    ),
                ],
            )
        ],
    )


def test_registration_message_to_payload_matches_spec_example():
    message = _sample_registration_message()

    assert message.to_payload() == {
        "deviceId": "birdbox01",
        "programs": [
            {
                "programId": "birdbox",
                "name": "Bird Box",
                "widgets": [
                    {"widgetId": "camera", "type": "Video", "capabilities": ["display"]},
                    {"widgetId": "snapshot", "type": "Button", "capabilities": ["command"]},
                ],
            }
        ],
    }


def test_registration_message_from_payload_missing_device_id_raises():
    with pytest.raises(ValueError, match="deviceId"):
        RegistrationMessage.from_payload({"programs": []})


def test_registration_message_programs_default_to_empty_list():
    message = RegistrationMessage.from_payload({"deviceId": "birdbox01"})

    assert message.programs == []


def test_registration_message_fingerprint_defaults_to_none():
    message = RegistrationMessage(device_id="birdbox01", programs=[])
    assert message.fingerprint is None


def test_registration_message_omits_fingerprint_key_when_none():
    message = RegistrationMessage(device_id="birdbox01", programs=[])
    assert "fingerprint" not in message.to_payload()


def test_registration_message_includes_fingerprint_when_present():
    """The registration payload carries the device fingerprint when there is one."""
    fingerprint = {
        "deviceType": "raspberrypi",
        "hostname": "birdbox01",
        "machineId": "b3974e6d1f8f4d53971c7a889af4a64f",
        "serialNumber": "10000000abcdef12",
    }
    message = RegistrationMessage(device_id="birdbox01", programs=[], fingerprint=fingerprint)

    assert message.to_payload()["fingerprint"] == fingerprint


def test_registration_message_from_payload_reads_fingerprint():
    fingerprint = {"deviceType": "linux", "hostname": "server01", "machineId": None, "serialNumber": None}

    message = RegistrationMessage.from_payload({"deviceId": "server01", "fingerprint": fingerprint})

    assert message.fingerprint == fingerprint


def test_registration_message_from_payload_without_fingerprint_defaults_to_none():
    """Backward compatibility: a payload from before fingerprints existed still parses."""
    message = RegistrationMessage.from_payload({"deviceId": "birdbox01", "programs": []})

    assert message.fingerprint is None


def test_registration_message_round_trips_through_envelope():
    original = _sample_registration_message()

    restored = RegistrationMessage.from_envelope(original.to_envelope())

    assert restored == original
    assert restored.to_envelope().message_type == MESSAGE_TYPE_REGISTRATION


def test_registration_message_round_trips_through_json():
    original = _sample_registration_message()

    restored = RegistrationMessage.from_json(original.to_json())

    assert restored == original


# --- parse_message dispatcher ---------------------------------------------


def test_known_message_types_contains_all_v1_types():
    # The five V1 types, plus `signal` (WebRTC Phase 1, the one additive change
    # WebRTC_Architecture.md 11.1 sanctions).
    assert KNOWN_MESSAGE_TYPES == {
        MESSAGE_TYPE_RUNTIME,
        MESSAGE_TYPE_COMMAND,
        MESSAGE_TYPE_LIFECYCLE,
        MESSAGE_TYPE_NOTIFICATION,
        MESSAGE_TYPE_REGISTRATION,
        "signal",
    }


@pytest.mark.parametrize("message_type", list(KNOWN_MESSAGE_TYPES))
def test_is_known_message_type_true_for_v1_types(message_type):
    assert is_known_message_type(message_type) is True


def test_is_known_message_type_false_for_unknown_type():
    assert is_known_message_type("futureMessageType") is False


def test_parse_message_dispatches_runtime():
    envelope = RuntimeMessage(binding=BINDING, value=22.5).to_envelope()

    parsed = parse_message(envelope)

    assert isinstance(parsed, RuntimeMessage)
    assert parsed.value == 22.5


def test_parse_message_dispatches_command():
    envelope = CommandMessage(binding=BINDING, action="press").to_envelope()

    parsed = parse_message(envelope)

    assert isinstance(parsed, CommandMessage)
    assert parsed.action == "press"


def test_parse_message_dispatches_lifecycle():
    envelope = LifecycleMessage.first_user_connected().to_envelope()

    parsed = parse_message(envelope)

    assert isinstance(parsed, LifecycleMessage)
    assert parsed.event == LIFECYCLE_FIRST_USER_CONNECTED


def test_parse_message_dispatches_notification():
    envelope = NotificationMessage.warning("birdbox01", "birdbox", "Storage 90% full").to_envelope()

    parsed = parse_message(envelope)

    assert isinstance(parsed, NotificationMessage)
    assert parsed.severity == SEVERITY_WARNING


def test_parse_message_dispatches_registration():
    envelope = _sample_registration_message().to_envelope()

    parsed = parse_message(envelope)

    assert isinstance(parsed, RegistrationMessage)
    assert parsed.device_id == "birdbox01"


def test_parse_message_returns_none_for_unknown_type():
    envelope = Envelope(message_type="futureMessageType", payload={"anything": True})

    assert parse_message(envelope) is None


def test_parse_message_raises_for_known_type_with_invalid_payload():
    envelope = Envelope(message_type=MESSAGE_TYPE_RUNTIME, payload={})

    with pytest.raises(ValueError):
        parse_message(envelope)


def test_parse_message_works_end_to_end_from_json():
    raw = CommandMessage(binding=BINDING, action="press").to_json()

    parsed = parse_message(Envelope.from_json(raw))

    assert isinstance(parsed, CommandMessage)
    assert parsed == CommandMessage(binding=BINDING, action="press")


# --- SignalMessage (WebRTC Phase 1; WebRTC_Architecture.md 7.3) ---------------

from pihub.protocol.messages import (  # noqa: E402
    MESSAGE_TYPE_SIGNAL,
    SIGNAL_ANSWER,
    SIGNAL_CANDIDATE,
    SIGNAL_CAPABILITIES,
    SIGNAL_CLOSE,
    SIGNAL_KINDS,
    SIGNAL_OFFER,
    SIGNAL_OPEN,
    SIGNAL_RESYNC,
    SIGNAL_STATE,
    SignalMessage,
)


def test_signal_is_a_known_message_type():
    assert MESSAGE_TYPE_SIGNAL == "signal"
    assert is_known_message_type("signal")


@pytest.mark.parametrize(
    "message",
    [
        SignalMessage(SIGNAL_OFFER, "psid1", {"sdp": "v=0\r\n..."}),
        SignalMessage(SIGNAL_ANSWER, "psid1", {"sdp": "v=0\r\n..."}),
        SignalMessage(
            SIGNAL_OPEN,
            "psid1",
            {"expiresAt": "2026-09-19T10:00:00Z", "ttlSeconds": 900, "grants": ["data"], "iceServers": []},
        ),
        SignalMessage(
            SIGNAL_CANDIDATE,
            "psid1",
            {"candidates": [{"candidate": "candidate:1 1 udp 1 1.2.3.4 5 typ host", "sdpMid": "0", "sdpMLineIndex": 0}]},
        ),
        SignalMessage(SIGNAL_STATE, "psid1", {"state": "connected", "path": "direct"}),
        SignalMessage(SIGNAL_CLOSE, "psid1", {"reason": "browser_closed"}),
        SignalMessage(SIGNAL_CAPABILITIES, None, {"webrtc": {"dataChannel": True}, "limits": {"maxViewerSessions": 5}}),
        SignalMessage(SIGNAL_RESYNC, None, {"sessions": []}),
    ],
)
def test_signal_round_trips_through_json_and_parse_message(message):
    parsed = parse_message(Envelope.from_json(message.to_json()))
    assert parsed == message


def test_signal_without_a_session_has_no_session_key_on_the_wire():
    payload = SignalMessage(SIGNAL_CAPABILITIES, None, {"limits": {}}).to_payload()
    assert "viewerSessionId" not in payload
    assert payload["kind"] == "capabilities"


def test_signal_payload_carries_the_session_id_and_data_at_the_top_level():
    payload = SignalMessage(SIGNAL_OFFER, "abc", {"sdp": "x"}).to_payload()
    assert payload == {"kind": "offer", "viewerSessionId": "abc", "sdp": "x"}


def test_signal_missing_kind_is_an_invalid_payload():
    with pytest.raises(ValueError):
        SignalMessage.from_payload({"viewerSessionId": "abc"})
    with pytest.raises(ValueError):
        SignalMessage.from_payload({"kind": ""})


def test_signal_session_id_must_be_a_string():
    with pytest.raises(ValueError):
        SignalMessage.from_payload({"kind": "offer", "viewerSessionId": 7})


def test_an_unknown_signal_kind_is_carried_not_rejected():
    message = SignalMessage.from_payload({"kind": "from-the-future", "viewerSessionId": "a", "x": 1})
    assert message.kind == "from-the-future" and message.kind not in SIGNAL_KINDS
    assert message.data == {"x": 1}


def test_adding_signal_changed_no_existing_message_type():
    assert {"runtime", "command", "lifecycle", "notification", "registration"} <= KNOWN_MESSAGE_TYPES


def test_media_and_usage_signal_kinds_round_trip_and_are_known():
    from pihub.protocol.messages import SIGNAL_KINDS, SIGNAL_MEDIA, SIGNAL_USAGE

    assert {SIGNAL_MEDIA, SIGNAL_USAGE} <= SIGNAL_KINDS
    for message in (
        SignalMessage(SIGNAL_MEDIA, "s1", {"action": "grant"}),
        SignalMessage(SIGNAL_MEDIA, "s1", {"action": "release"}),
        SignalMessage(SIGNAL_USAGE, "s1", {"mediaActive": False}),
    ):
        assert parse_message(Envelope.from_json(message.to_json())) == message
