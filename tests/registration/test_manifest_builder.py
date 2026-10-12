import socket
from typing import List

import pytest

from pihub import Device, Program
from pihub.device_identity import DeviceFingerprint
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import ProgramRegistration, RegistrationMessage, WidgetRegistration
from pihub.registration.manifest_builder import RegistrableProgram, build_device_manifest


class FakeProgram:
    """A minimal duck-typed stand-in, satisfying `RegistrableProgram` directly."""

    def __init__(self, id: str, registration: ProgramRegistration) -> None:
        self.id = id
        self._registration = registration

    def to_registration(self) -> ProgramRegistration:
        return self._registration


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)


# --- pure unit behaviour, via a duck-typed fake program ---------------------


def test_no_programs_produces_empty_manifest():
    message = build_device_manifest("birdbox01", [])

    assert message == RegistrationMessage(device_id="birdbox01", programs=[])


def test_single_program_is_included():
    registration = ProgramRegistration(program_id="birdbox", name="Bird Box", widgets=[])
    program = FakeProgram(id="birdbox", registration=registration)

    message = build_device_manifest("birdbox01", [program])

    assert message.device_id == "birdbox01"
    assert message.programs == [registration]


def test_multiple_programs_preserve_order():
    first = ProgramRegistration(program_id="birdbox", name="Bird Box", widgets=[])
    second = ProgramRegistration(program_id="weather", name="Weather Station", widgets=[])

    message = build_device_manifest(
        "hub01",
        [FakeProgram(id="birdbox", registration=first), FakeProgram(id="weather", registration=second)],
    )

    assert message.programs == [first, second]


def test_accepts_any_iterable_not_just_a_list():
    registration = ProgramRegistration(program_id="birdbox", name="Bird Box", widgets=[])
    program = FakeProgram(id="birdbox", registration=registration)

    def generator():
        yield program

    message = build_device_manifest("birdbox01", generator())

    assert message.programs == [registration]


def test_fake_program_satisfies_registrable_program_protocol():
    program = FakeProgram(id="birdbox", registration=ProgramRegistration(program_id="birdbox", name="Bird Box"))
    assert isinstance(program, RegistrableProgram)


# --- return type / serialization --------------------------------------------


def test_returns_a_registration_message_instance():
    message = build_device_manifest("birdbox01", [])
    assert isinstance(message, RegistrationMessage)


def test_result_round_trips_through_json():
    registration = ProgramRegistration(
        program_id="birdbox",
        name="Bird Box",
        widgets=[WidgetRegistration(widget_id="camera", type="Video", capabilities=["display"])],
    )
    message = build_device_manifest("birdbox01", [FakeProgram(id="birdbox", registration=registration)])

    round_tripped = RegistrationMessage.from_json(message.to_json())

    assert round_tripped == message


# --- integration: real Device + real Program + real Widget classes ---------


def test_matches_protocol_spec_registration_example():
    """Mirrors the protocol's registration message example,
    using a real Program with real Output/Command widgets - the manifest
    is generated entirely through existing classes, never hand-built."""
    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(id="camera", type="Video")
    birdbox.command(id="snapshot", type="Button")

    message = build_device_manifest("birdbox01", [birdbox])

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


def test_matches_widget_metadata_registration_prompt_example():
    """End-to-end proof of the documented widget-metadata registration JSON: Device -> Program.output()
    with StringProperty declarations -> build_device_manifest() produces
    exactly the documented `propertySchema` / `defaultProperties` shape,
    generated entirely through the existing property system."""
    from pihub.properties import StringProperty

    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(
        id="temperature",
        type="Gauge",
        properties=[
            StringProperty("title", default="Temperature"),
            StringProperty("units", default="°C"),
        ],
    )

    message = build_device_manifest("birdbox01", [birdbox])

    assert message.to_payload() == {
        "deviceId": "birdbox01",
        "programs": [
            {
                "programId": "birdbox",
                "name": "Bird Box",
                "widgets": [
                    {
                        "widgetId": "temperature",
                        "type": "Gauge",
                        "capabilities": ["display"],
                        "propertySchema": [
                            {"name": "title", "label": "Title", "control": "text"},
                            {"name": "units", "label": "Units", "control": "text"},
                        ],
                        "defaultProperties": {"title": "Temperature", "units": "°C"},
                    }
                ],
            }
        ],
    }


def test_property_metadata_round_trips_through_json():
    from pihub.properties import StringProperty

    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(
        id="temperature", type="Gauge", properties=[StringProperty("title", default="Temperature")]
    )
    message = build_device_manifest("birdbox01", [birdbox])

    restored = RegistrationMessage.from_json(message.to_json())

    widget = restored.programs[0].widgets[0]
    assert widget.property_schema == [{"name": "title", "label": "Title", "control": "text"}]
    assert widget.default_properties == {"title": "Temperature"}


def test_device_start_delegates_to_build_device_manifest():
    """Device.start() must produce exactly what this module builds directly -
    proving Device actually uses this builder rather than duplicating it.

    The fingerprint is excluded from the comparison: `device.start()`
    collects a real, host-dependent one automatically (the device identity
    rules), while `build_device_manifest()`
    called directly here has none - the two are only expected to agree
    on device_id/programs.
    """
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(id="temperature", type="Gauge")
    device.add_program(birdbox)

    expected = build_device_manifest("birdbox01", [birdbox])

    device.start()

    assert len(transport.sent) == 1
    sent_message = RegistrationMessage.from_envelope(Envelope.from_json(transport.sent[0]))
    assert sent_message.device_id == expected.device_id
    assert sent_message.programs == expected.programs


def test_device_with_multiple_programs_and_widgets_end_to_end():
    transport = RecordingTransport()
    device = Device(id="hub01", transport=transport)

    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(id="camera", type="Video")
    weather = Program(id="weather", name="Weather Station")
    weather.output(id="temperature", type="Gauge")
    weather.command(id="calibrate", type="Button")

    device.add_program(birdbox)
    device.add_program(weather)
    device.start()

    message = RegistrationMessage.from_envelope(Envelope.from_json(transport.sent[0]))
    assert [p.program_id for p in message.programs] == ["birdbox", "weather"]
    assert [w.widget_id for w in message.programs[1].widgets] == ["temperature", "calibrate"]


def test_device_with_no_programs_sends_empty_manifest():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)

    device.start()

    message = RegistrationMessage.from_envelope(Envelope.from_json(transport.sent[0]))
    expected = build_device_manifest("birdbox01", [])
    assert message.device_id == expected.device_id
    assert message.programs == expected.programs


# --- device identity / fingerprint -----------------------------------------


def test_device_start_automatically_attaches_a_fingerprint():
    """Fingerprint collection must be automatic - no call to `collect_fingerprint()` appears anywhere in
    this test, yet the sent registration carries one."""
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)

    device.start()

    message = RegistrationMessage.from_envelope(Envelope.from_json(transport.sent[0]))
    assert message.fingerprint is not None
    assert set(message.fingerprint) == {"deviceType", "hostname", "machineId", "serialNumber"}
    assert message.fingerprint["hostname"] == socket.gethostname()


def test_build_device_manifest_without_a_fingerprint_omits_it():
    message = build_device_manifest("birdbox01", [])
    assert message.fingerprint is None
    assert "fingerprint" not in message.to_payload()


def test_build_device_manifest_attaches_a_given_fingerprint():
    fingerprint = DeviceFingerprint(
        device_type="raspberrypi", hostname="birdbox01", machine_id="abc123", serial_number="xyz789"
    )

    message = build_device_manifest("birdbox01", [], fingerprint)

    assert message.fingerprint == {
        "deviceType": "raspberrypi",
        "hostname": "birdbox01",
        "machineId": "abc123",
        "serialNumber": "xyz789",
    }
