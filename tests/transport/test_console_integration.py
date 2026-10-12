import json

from pihub import ConsoleTransport, Device, Program
from pihub.protocol.messages import NotificationMessage, RegistrationMessage, RuntimeMessage


def _extract_json(printed: str) -> dict:
    _, body = printed.split("\n", 1)
    return json.loads(body.strip())


def test_prompt_example_runs_end_to_end_without_networking(capsys):
    """The exact example from the task prompt: no explicit connect() call."""
    device = Device(id="birdbox01", transport=ConsoleTransport())

    birdbox = Program(id="birdbox", name="Bird Box")

    temperature = birdbox.output(id="temperature", type="Gauge")

    device.add_program(birdbox)

    device.start()

    temperature.set(22.5)

    captured = capsys.readouterr().out
    assert "[ConsoleTransport] registration" in captured
    assert "[ConsoleTransport] runtime" in captured
    assert '"deviceId": "birdbox01"' in captured
    assert '"value": 22.5' in captured


def test_registration_message_sent_through_device_start(capsys):
    device = Device(id="birdbox01", transport=ConsoleTransport())
    birdbox = Program(id="birdbox", name="Bird Box")
    birdbox.output(id="camera", type="Video")
    device.add_program(birdbox)

    device.start()

    printed = _extract_json(capsys.readouterr().out)
    message = RegistrationMessage.from_payload(printed["payload"])
    assert message.device_id == "birdbox01"
    assert message.programs[0].program_id == "birdbox"
    assert message.programs[0].widgets[0].widget_id == "camera"


def test_runtime_message_sent_through_output_set(capsys):
    device = Device(id="birdbox01", transport=ConsoleTransport())
    birdbox = Program(id="birdbox", name="Bird Box")
    temperature = birdbox.output(id="temperature", type="Gauge")
    device.add_program(birdbox)
    device.start()
    capsys.readouterr()  # discard the registration output

    temperature.set(22.5)

    printed = _extract_json(capsys.readouterr().out)
    message = RuntimeMessage.from_payload(printed["payload"])
    assert message.binding.canonical_id == "birdbox01.birdbox.temperature"
    assert message.value == 22.5


def test_notification_message_sent_through_program_info(capsys):
    device = Device(id="birdbox01", transport=ConsoleTransport())
    birdbox = Program(id="birdbox", name="Bird Box")
    device.add_program(birdbox)
    device.start()
    capsys.readouterr()

    birdbox.info("Snapshot captured")

    printed = _extract_json(capsys.readouterr().out)
    message = NotificationMessage.from_payload(printed["payload"])
    assert message.device_id == "birdbox01"
    assert message.program_id == "birdbox"
    assert message.severity == "info"
    assert message.message == "Snapshot captured"


def test_device_start_reconnects_a_disconnected_console_transport(capsys):
    """`device.start()` now connects the transport itself as
    its first step - a `ConsoleTransport` that was manually disconnected
    beforehand no longer blocks `start()`, it gets reconnected."""
    transport = ConsoleTransport()
    transport.disconnect()
    device = Device(id="birdbox01", transport=transport)

    device.start()  # should not raise - it reconnects automatically

    assert transport.connected is True
    assert "[ConsoleTransport] registration" in capsys.readouterr().out
    device.stop()


def test_command_widget_and_lifecycle_flow_also_prints_readably(capsys):
    device = Device(id="birdbox01", transport=ConsoleTransport())
    birdbox = Program(id="birdbox", name="Bird Box")
    snapshot = birdbox.command(id="snapshot", type="Button")

    calls = []

    @snapshot.handler
    def take_snapshot(command):
        calls.append(command)
        birdbox.warning("Storage 90% full")

    device.add_program(birdbox)
    device.start()
    capsys.readouterr()

    from pihub.protocol.bindings import Binding
    from pihub.protocol.messages import CommandMessage

    binding = Binding(device_id="birdbox01", program_id="birdbox", widget_id="snapshot")
    device.handle_incoming(CommandMessage(binding=binding, action="press").to_json())

    assert len(calls) == 1
    captured = capsys.readouterr().out
    assert "[ConsoleTransport] notification" in captured
    assert '"severity": "warning"' in captured
