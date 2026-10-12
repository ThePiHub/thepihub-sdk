"""Tests for Device.simulate_command() / Device.simulate_lifecycle()."""

from typing import Any, List

import pytest

from pihub import Device, Program
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import CommandMessage, LifecycleMessage


class Recorder:
    def __init__(self) -> None:
        self.calls: List[Any] = []

    def __call__(self, payload: Any) -> None:
        self.calls.append(payload)


@pytest.fixture
def device() -> Device:
    return Device(id="birdbox01")


@pytest.fixture
def program(device: Device) -> Program:
    birdbox = Program(id="birdbox", name="Bird Box")
    device.add_program(birdbox)
    return birdbox


# --- simulate_command(): action-style -------------------------------------


def test_simulate_command_with_action_invokes_handler(device, program):
    snapshot = program.command(id="snapshot", type="Button")
    recorder = Recorder()

    @snapshot.handler
    def take_snapshot(command):
        recorder(command)

    device.simulate_command(program_id="birdbox", widget_id="snapshot", action="press")

    assert len(recorder.calls) == 1
    command = recorder.calls[0]
    assert command.action == "press"
    assert command.values is None
    assert command.binding.canonical_id == "birdbox01.birdbox.snapshot"


def test_simulate_command_handler_receives_command_message_instance(device, program):
    snapshot = program.command(id="snapshot", type="Button")
    recorder = Recorder()
    snapshot.handler(recorder)

    device.simulate_command(program_id="birdbox", widget_id="snapshot", action="press")

    assert isinstance(recorder.calls[0], CommandMessage)


# --- simulate_command(): values-style --------------------------------------


def test_simulate_command_with_values_invokes_handler(device, program):
    joystick = program.command(id="joystick", type="Joystick")
    recorder = Recorder()

    @joystick.handler
    def move(command):
        recorder(command)

    device.simulate_command(program_id="birdbox", widget_id="joystick", values={"x": 50, "y": -20})

    assert len(recorder.calls) == 1
    command = recorder.calls[0]
    assert command.action is None
    assert command.values == {"x": 50, "y": -20}


# --- validation, reused from the existing CommandMessage --------------------


def test_simulate_command_requires_exactly_one_of_action_or_values(device, program):
    program.command(id="snapshot", type="Button")

    with pytest.raises(ValueError):
        device.simulate_command(program_id="birdbox", widget_id="snapshot")

    with pytest.raises(ValueError):
        device.simulate_command(
            program_id="birdbox", widget_id="snapshot", action="press", values={"x": 1}
        )


def test_simulate_command_for_unregistered_widget_does_not_raise(device, program):
    # No handler registered for "unknown" - EventBus publish with no
    # subscribers is a no-op, so this should not raise.
    device.simulate_command(program_id="birdbox", widget_id="unknown", action="press")


def test_simulate_command_uses_the_devices_own_id(program):
    device = Device(id="greenhouse01")
    other_program = Program(id="greenhouse", name="Greenhouse")
    button = other_program.command(id="waterPlants", type="Button")
    recorder = Recorder()
    button.handler(recorder)
    device.add_program(other_program)

    device.simulate_command(program_id="greenhouse", widget_id="waterPlants", action="press")

    assert recorder.calls[0].binding.device_id == "greenhouse01"


# --- simulate_lifecycle(): each documented event -----------------------------


def test_simulate_lifecycle_first_user_connected(device, program):
    recorder = Recorder()
    program.on_first_user_connected(recorder)

    device.simulate_lifecycle("firstUserConnected")

    assert recorder.calls == [{}]


def test_simulate_lifecycle_last_user_disconnected(device, program):
    recorder = Recorder()
    program.on_last_user_disconnected(recorder)

    device.simulate_lifecycle("lastUserDisconnected")

    assert recorder.calls == [{}]


def test_simulate_lifecycle_viewer_count_changed(device, program):
    recorder = Recorder()
    program.on_viewer_count_changed(recorder)

    device.simulate_lifecycle("viewerCountChanged", viewerCount=3)

    assert recorder.calls == [{"viewerCount": 3}]


def test_simulate_lifecycle_dashboard_opened(device):
    recorder = Recorder()
    device.events.subscribe("lifecycle.dashboardOpened", recorder)

    device.simulate_lifecycle("dashboardOpened", dashboardId="birdbox")

    assert recorder.calls == [{"dashboardId": "birdbox"}]


def test_simulate_lifecycle_dashboard_closed(device):
    recorder = Recorder()
    device.events.subscribe("lifecycle.dashboardClosed", recorder)

    device.simulate_lifecycle("dashboardClosed", dashboardId="birdbox")

    assert recorder.calls == [{"dashboardId": "birdbox"}]


def test_simulate_lifecycle_with_no_kwargs_produces_empty_data(device):
    recorder = Recorder()
    device.events.subscribe("lifecycle.firstUserConnected", recorder)

    device.simulate_lifecycle("firstUserConnected")

    assert recorder.calls == [{}]


def test_simulate_lifecycle_supports_event_names_beyond_the_known_five(device):
    """The protocol is extensible: a known
    message TYPE (lifecycle) with a not-yet-catalogued event name must
    still flow through untouched, not be rejected."""
    recorder = Recorder()
    device.events.subscribe("lifecycle.somethingNew", recorder)

    device.simulate_lifecycle("somethingNew", foo="bar")

    assert recorder.calls == [{"foo": "bar"}]


# --- integration: the full pipeline, per the prompt's diagram ---------------


def test_simulate_command_routes_through_handle_incoming(device, program):
    program.command(id="snapshot", type="Button")
    calls: List[str] = []
    original = device.handle_incoming

    def spy(data: str) -> None:
        calls.append(data)
        original(data)

    device.handle_incoming = spy

    device.simulate_command(program_id="birdbox", widget_id="snapshot", action="press")

    assert len(calls) == 1
    envelope = Envelope.from_json(calls[0])
    assert envelope.message_type == "command"
    message = CommandMessage.from_envelope(envelope)
    assert message.action == "press"
    assert message.binding.canonical_id == "birdbox01.birdbox.snapshot"


def test_simulate_lifecycle_routes_through_handle_incoming(device):
    calls: List[str] = []
    original = device.handle_incoming

    def spy(data: str) -> None:
        calls.append(data)
        original(data)

    device.handle_incoming = spy

    device.simulate_lifecycle("viewerCountChanged", viewerCount=5)

    assert len(calls) == 1
    envelope = Envelope.from_json(calls[0])
    assert envelope.message_type == "lifecycle"
    message = LifecycleMessage.from_envelope(envelope)
    assert message.event == "viewerCountChanged"
    assert message.data == {"viewerCount": 5}


def test_simulate_command_has_no_path_to_eventbus_other_than_handle_incoming(device, program):
    """If simulate_command() ever gains a second, direct path to
    `events.publish()`, this must fail: stubbing handle_incoming() to a
    no-op should silence the handler completely."""
    snapshot = program.command(id="snapshot", type="Button")
    recorder = Recorder()
    snapshot.handler(recorder)

    device.handle_incoming = lambda data: None

    device.simulate_command(program_id="birdbox", widget_id="snapshot", action="press")

    assert recorder.calls == []


def test_simulate_lifecycle_has_no_path_to_eventbus_other_than_handle_incoming(device, program):
    recorder = Recorder()
    program.on_first_user_connected(recorder)

    device.handle_incoming = lambda data: None

    device.simulate_lifecycle("firstUserConnected")

    assert recorder.calls == []
