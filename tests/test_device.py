import json
from typing import List

import pytest

from pihub import Device
from pihub.protocol.bindings import Binding
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import (
    CommandMessage,
    LifecycleMessage,
    MESSAGE_TYPE_REGISTRATION,
    NotificationMessage,
    ProgramRegistration,
    RegistrationMessage,
    WidgetRegistration,
)


class FakeProgram:
    """Minimal stand-in for the not-yet-implemented Program class.

    Satisfies `RegistrableProgram`: an `id` plus `to_registration()`
    returning the existing `ProgramRegistration` protocol dataclass.
    """

    def __init__(self, id: str, name: str, widgets: List[WidgetRegistration] = None) -> None:
        self.id = id
        self.name = name
        self.widgets = widgets or []

    def to_registration(self) -> ProgramRegistration:
        return ProgramRegistration(program_id=self.id, name=self.name, widgets=self.widgets)


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)


class Recorder:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, payload) -> None:
        self.calls.append(payload)


def _lifecycle_json(event: str, **data) -> str:
    return LifecycleMessage(event=event, data=data).to_json()


def _command_json(device_id: str, program_id: str, widget_id: str, **kwargs) -> str:
    binding = Binding(device_id=device_id, program_id=program_id, widget_id=widget_id)
    return CommandMessage(binding=binding, **kwargs).to_json()


# --- construction -----------------------------------------------------


def test_device_holds_its_id():
    device = Device(id="birdbox01")
    assert device.id == "birdbox01"


def test_device_owns_an_event_bus():
    from pihub.events.bus import EventBus

    device = Device(id="birdbox01")
    assert isinstance(device.events, EventBus)


# --- add_program --------------------------------------------------------


def test_add_program_stores_program():
    device = Device(id="birdbox01")
    program = FakeProgram(id="birdbox", name="Bird Box")

    device.add_program(program)

    assert device._programs["birdbox"] is program


def test_add_program_rejects_duplicate_id():
    device = Device(id="birdbox01")
    device.add_program(FakeProgram(id="birdbox", name="Bird Box"))

    with pytest.raises(ValueError):
        device.add_program(FakeProgram(id="birdbox", name="Bird Box Again"))


def test_add_program_after_start_raises():
    device = Device(id="birdbox01")
    device.start()

    with pytest.raises(RuntimeError):
        device.add_program(FakeProgram(id="birdbox", name="Bird Box"))


# --- start / registration -----------------------------------------------


def test_start_with_no_transport_does_not_raise():
    device = Device(id="birdbox01")
    device.start()  # should not raise


def test_start_sends_registration_message_via_transport():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)

    device.start()

    assert len(transport.sent) == 1
    envelope = Envelope.from_json(transport.sent[0])
    assert envelope.message_type == MESSAGE_TYPE_REGISTRATION
    message = RegistrationMessage.from_envelope(envelope)
    assert message.device_id == "birdbox01"
    assert message.programs == []


def test_start_includes_registered_programs_and_widgets():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    temperature = WidgetRegistration(
        widget_id="temperature", type="Gauge", capabilities=["display"]
    )
    snapshot = WidgetRegistration(widget_id="snapshot", type="Button", capabilities=["command"])
    device.add_program(FakeProgram(id="birdbox", name="Bird Box", widgets=[temperature, snapshot]))

    device.start()

    envelope = Envelope.from_json(transport.sent[0])
    message = RegistrationMessage.from_envelope(envelope)
    assert len(message.programs) == 1
    program = message.programs[0]
    assert program.program_id == "birdbox"
    assert program.name == "Bird Box"
    assert program.widgets == [temperature, snapshot]


def test_start_twice_raises():
    device = Device(id="birdbox01")
    device.start()

    with pytest.raises(RuntimeError):
        device.start()


def test_restart_after_stop_sends_registration_again():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)

    device.start()
    device.stop()
    device.start()

    assert len(transport.sent) == 2


# --- stop -----------------------------------------------------------------


def test_stop_without_start_raises():
    device = Device(id="birdbox01")

    with pytest.raises(RuntimeError):
        device.stop()


def test_stop_after_start_succeeds():
    device = Device(id="birdbox01")
    device.start()

    device.stop()  # should not raise


def test_stop_twice_is_safe():
    """Calling
    `stop()` multiple times must be safe - distinct from `stop()` before
    ever starting, which still raises (see `test_stop_without_start_raises`)."""
    device = Device(id="birdbox01")
    device.start()

    device.stop()
    device.stop()  # should not raise


def test_stop_three_times_is_still_safe():
    device = Device(id="birdbox01")
    device.start()

    device.stop()
    device.stop()
    device.stop()  # should not raise


# --- optional transport capabilities (connect/receive/disconnect) ----------


def test_start_and_stop_work_with_a_transport_that_only_supports_send():
    """`RecordingTransport` has no `connect`/`receive`/`disconnect` at all -
    a transport this minimal (satisfying only `device.py`'s own `Transport`
    protocol) must keep working exactly as before; the runtime service and
    auto-connect/disconnect are opt-in extras, not requirements."""
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)

    device.start()  # should not raise despite no connect()/receive()
    device.stop()  # should not raise despite no disconnect()

    assert device._runtime_service is None


# --- handle_incoming: lifecycle -------------------------------------------


def test_handle_incoming_lifecycle_publishes_bare_event():
    device = Device(id="birdbox01")
    recorder = Recorder()
    device.events.subscribe("lifecycle.firstUserConnected", recorder)

    device.handle_incoming(_lifecycle_json("firstUserConnected"))

    assert recorder.calls == [{}]


def test_handle_incoming_lifecycle_publishes_event_data():
    device = Device(id="birdbox01")
    recorder = Recorder()
    device.events.subscribe("lifecycle.viewerCountChanged", recorder)

    device.handle_incoming(_lifecycle_json("viewerCountChanged", viewerCount=3))

    assert recorder.calls == [{"viewerCount": 3}]


# --- handle_incoming: command ----------------------------------------------


def test_handle_incoming_command_publishes_to_widget_scoped_event():
    device = Device(id="birdbox01")
    recorder = Recorder()
    device.events.subscribe("command.snapshot", recorder)

    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="snapshot", action="press")
    )

    assert len(recorder.calls) == 1
    command = recorder.calls[0]
    assert isinstance(command, CommandMessage)
    assert command.action == "press"
    assert command.binding.canonical_id == "birdbox01.birdbox.snapshot"


def test_handle_incoming_command_does_not_leak_to_other_widgets():
    device = Device(id="birdbox01")
    recorder = Recorder()
    device.events.subscribe("command.alarm", recorder)

    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="snapshot", action="press")
    )

    assert recorder.calls == []


# --- handle_incoming: messages device should ignore ------------------------


def test_handle_incoming_ignores_unknown_message_type():
    device = Device(id="birdbox01")
    envelope = Envelope(message_type="futureMessageType", payload={})

    device.handle_incoming(envelope.to_json())  # should not raise


def test_handle_incoming_ignores_out_of_direction_notification_message():
    device = Device(id="birdbox01")
    recorder = Recorder()
    # Device has no notification.* handling of its own; make sure nothing
    # blows up and nothing is (incorrectly) published either.
    device.events.subscribe("notification.info", recorder)

    message = NotificationMessage.info(
        device_id="birdbox01", program_id="birdbox", message="hello"
    )
    device.handle_incoming(message.to_json())

    assert recorder.calls == []
