from typing import List

import pytest

from pihub import Device, Program
from pihub.properties import StringProperty
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import (
    CommandMessage,
    LifecycleMessage,
    NotificationMessage,
    RegistrationMessage,
    RuntimeMessage,
)
from pihub.widgets import Command, Output


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)


def _sent_messages(transport: RecordingTransport):
    return [Envelope.from_json(raw) for raw in transport.sent]


def _lifecycle_json(event: str, **data) -> str:
    return LifecycleMessage(event=event, data=data).to_json()


def _command_json(device_id: str, program_id: str, widget_id: str, **kwargs) -> str:
    from pihub.protocol.bindings import Binding

    binding = Binding(device_id=device_id, program_id=program_id, widget_id=widget_id)
    return CommandMessage(binding=binding, **kwargs).to_json()


# --- construction / widget registration -----------------------------------


def test_program_holds_id_and_name():
    program = Program(id="birdbox", name="Bird Box")
    assert program.id == "birdbox"
    assert program.name == "Bird Box"


def test_output_returns_widget_with_id_and_type():
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")
    assert temperature.id == "temperature"
    assert temperature.type == "Gauge"


def test_command_returns_widget_with_id_and_type():
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button")
    assert snapshot.id == "snapshot"
    assert snapshot.type == "Button"


def test_duplicate_widget_id_across_output_raises():
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="temperature", type="Gauge")

    with pytest.raises(ValueError):
        program.output(id="temperature", type="Gauge")


def test_duplicate_widget_id_between_output_and_command_raises():
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="snapshot", type="Gauge")

    with pytest.raises(ValueError):
        program.command(id="snapshot", type="Button")


# --- to_registration --------------------------------------------------------


def test_to_registration_includes_output_and_command_widgets_with_capabilities():
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="temperature", type="Gauge")
    program.command(id="snapshot", type="Button")

    registration = program.to_registration()

    assert registration.program_id == "birdbox"
    assert registration.name == "Bird Box"
    widgets_by_id = {w.widget_id: w for w in registration.widgets}
    assert widgets_by_id["temperature"].type == "Gauge"
    assert widgets_by_id["temperature"].capabilities == ["display"]
    assert widgets_by_id["snapshot"].type == "Button"
    assert widgets_by_id["snapshot"].capabilities == ["command"]


def test_to_registration_with_no_widgets_is_empty():
    program = Program(id="birdbox", name="Bird Box")
    registration = program.to_registration()
    assert registration.widgets == []


# --- attachment via Device.add_program --------------------------------------


def test_add_program_attaches_device():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")

    device.add_program(program)

    assert program._device is device


def test_attaching_already_attached_program_to_another_device_raises():
    device1 = Device(id="birdbox01")
    device2 = Device(id="birdbox02")
    program = Program(id="birdbox", name="Bird Box")

    device1.add_program(program)

    with pytest.raises(RuntimeError):
        device2.add_program(program)


def test_device_start_sends_registration_built_from_program_widgets():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="temperature", type="Gauge")
    device.add_program(program)

    device.start()

    envelope = _sent_messages(transport)[0]
    message = RegistrationMessage.from_envelope(envelope)
    assert message.programs[0].program_id == "birdbox"
    assert message.programs[0].widgets[0].widget_id == "temperature"


# --- output.set() / set_values() --------------------------------------------


def test_output_set_before_attach_raises():
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")

    with pytest.raises(RuntimeError):
        temperature.set(22.5)


def test_output_set_sends_runtime_message_with_binding_and_value():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")
    device.add_program(program)

    temperature.set(22.5)

    envelope = _sent_messages(transport)[0]
    message = RuntimeMessage.from_envelope(envelope)
    assert message.binding.canonical_id == "birdbox01.birdbox.temperature"
    assert message.value == 22.5
    assert message.values is None


def test_output_set_values_sends_runtime_message_with_values():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    program = Program(id="birdbox", name="Bird Box")
    system = program.output(id="system", type="SystemHealth")
    device.add_program(program)

    system.set_values({"cpu": 12, "memory": 40, "disk": 75})

    envelope = _sent_messages(transport)[0]
    message = RuntimeMessage.from_envelope(envelope)
    assert message.binding.canonical_id == "birdbox01.birdbox.system"
    assert message.value is None
    assert message.values == {"cpu": 12, "memory": 40, "disk": 75}


def test_output_set_before_device_start_still_sends_immediately():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")
    device.add_program(program)

    temperature.set(1.0)  # sent before device.start()

    assert len(transport.sent) == 1


# --- command().handler -------------------------------------------------------


def test_command_handler_registered_before_attach_fires_on_incoming_command():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button")

    received = []

    @snapshot.handler
    def take_snapshot(command):
        received.append(command)

    device.add_program(program)  # attach happens after handler registration

    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="snapshot", action="press")
    )

    assert len(received) == 1
    assert isinstance(received[0], CommandMessage)
    assert received[0].action == "press"


def test_command_handler_registered_after_attach_fires_on_incoming_command():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    device.add_program(program)  # attach happens before the widget/handler exist

    snapshot = program.command(id="snapshot", type="Button")
    received = []

    @snapshot.handler
    def take_snapshot(command):
        received.append(command)

    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="snapshot", action="press")
    )

    assert len(received) == 1


def test_command_handler_returns_the_original_function():
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button")

    def take_snapshot(command):
        pass

    assert snapshot.handler(take_snapshot) is take_snapshot


def test_command_for_other_widget_does_not_trigger_handler():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button")
    device.add_program(program)

    received = []

    @snapshot.handler
    def take_snapshot(command):
        received.append(command)

    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="alarm", action="press")
    )

    assert received == []


# --- notifications -----------------------------------------------------------


def test_info_before_attach_raises():
    program = Program(id="birdbox", name="Bird Box")
    with pytest.raises(RuntimeError):
        program.info("hello")


@pytest.mark.parametrize(
    "method, severity",
    [
        ("info", "info"),
        ("warning", "warning"),
        ("error", "error"),
    ],
)
def test_notification_methods_send_notification_message(method, severity):
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    program = Program(id="birdbox", name="Bird Box")
    device.add_program(program)

    getattr(program, method)("Storage 90% full")

    envelope = _sent_messages(transport)[0]
    message = NotificationMessage.from_envelope(envelope)
    assert message.device_id == "birdbox01"
    assert message.program_id == "birdbox"
    assert message.severity == severity
    assert message.message == "Storage 90% full"


def test_notification_methods_also_publish_on_event_bus():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    device.add_program(program)

    received = []
    device.events.subscribe("notification.warning", received.append)

    program.warning("Storage 90% full")

    assert len(received) == 1
    assert isinstance(received[0], NotificationMessage)
    assert received[0].message == "Storage 90% full"


# --- lifecycle decorators -----------------------------------------------------


def test_on_first_user_connected_registered_before_attach_fires_with_empty_payload():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")

    received = []

    @program.on_first_user_connected
    def start_camera(payload):
        received.append(payload)

    device.add_program(program)
    device.handle_incoming(_lifecycle_json("firstUserConnected"))

    assert received == [{}]


def test_on_last_user_disconnected_fires():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    received = []

    @program.on_last_user_disconnected
    def stop_camera(payload):
        received.append(payload)

    device.add_program(program)
    device.handle_incoming(_lifecycle_json("lastUserDisconnected"))

    assert received == [{}]


def test_on_viewer_count_changed_fires_with_count_payload():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    received = []

    @program.on_viewer_count_changed
    def viewers(payload):
        received.append(payload["viewerCount"])

    device.add_program(program)
    device.handle_incoming(_lifecycle_json("viewerCountChanged", viewerCount=3))

    assert received == [3]


def test_lifecycle_handler_registered_after_attach_fires():
    device = Device(id="birdbox01")
    program = Program(id="birdbox", name="Bird Box")
    device.add_program(program)

    received = []

    @program.on_first_user_connected
    def start_camera(payload):
        received.append(payload)

    device.handle_incoming(_lifecycle_json("firstUserConnected"))

    assert received == [{}]


def test_lifecycle_decorator_returns_the_original_function():
    program = Program(id="birdbox", name="Bird Box")

    def start_camera(payload):
        pass

    assert program.on_first_user_connected(start_camera) is start_camera


# --- end-to-end, mirroring the spec's "Example Program" ----------------------


def test_end_to_end_example_program_flow():
    transport = RecordingTransport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")

    temperature = birdbox.output(id="temperature", type="Gauge")
    snapshot = birdbox.command(id="snapshot", type="Button")

    notifications = []

    @snapshot.handler
    def take_snapshot(command):
        birdbox.info("Snapshot captured")

    viewer_events = []

    @birdbox.on_first_user_connected
    def start_camera(payload):
        viewer_events.append("connected")

    device.add_program(birdbox)
    device.start()
    temperature.set(22.5)

    device.handle_incoming(_lifecycle_json("firstUserConnected"))
    device.handle_incoming(
        _command_json(device_id="birdbox01", program_id="birdbox", widget_id="snapshot", action="press")
    )

    assert viewer_events == ["connected"]

    envelopes = _sent_messages(transport)
    types = [e.message_type for e in envelopes]
    assert types == ["registration", "runtime", "notification"]

    runtime_message = RuntimeMessage.from_envelope(envelopes[1])
    assert runtime_message.value == 22.5

    notification_message = NotificationMessage.from_envelope(envelopes[2])
    assert notification_message.message == "Snapshot captured"


# --- widgets/ integration: Output/Command come from pihub.widgets, ----------
# --- and properties=[...] is wired through to them. -------------------------


def test_output_returns_a_pihub_widgets_output_instance():
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")
    assert isinstance(temperature, Output)


def test_command_returns_a_pihub_widgets_command_instance():
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button")
    assert isinstance(snapshot, Command)


def test_output_properties_are_stored_on_the_widget():
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(
        id="temperature",
        type="Gauge",
        properties=[
            StringProperty("title", default="Temperature"),
            StringProperty("units", default="°C"),
        ],
    )

    assert temperature.property_schema() == [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"},
    ]
    assert temperature.property_defaults() == {"title": "Temperature", "units": "°C"}


def test_command_properties_are_stored_on_the_widget():
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command(id="snapshot", type="Button", properties=[StringProperty("label", default="Snap")])

    assert snapshot.property_schema() == [{"name": "label", "label": "Label", "control": "text"}]
    assert snapshot.property_defaults() == {"label": "Snap"}


def test_output_without_properties_defaults_to_empty():
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output(id="temperature", type="Gauge")
    assert temperature.properties == []
