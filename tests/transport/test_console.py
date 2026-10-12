import json

import pytest

from pihub.device import Transport
from pihub.transport.console import ConsoleTransport, TransportDisconnectedError


def _extract_json(printed: str) -> dict:
    """Pull the JSON body out of one `[ConsoleTransport] <label>\\n<body>\\n` block."""
    _, body = printed.split("\n", 1)
    return json.loads(body.strip())


# --- extends the existing Transport base class -------------------------


def test_console_transport_is_a_transport_subclass():
    assert issubclass(ConsoleTransport, Transport)


def test_console_transport_instance_satisfies_transport_protocol():
    assert isinstance(ConsoleTransport(), Transport)


# --- connect() / disconnect() -------------------------------------------


def test_starts_connected_by_default():
    transport = ConsoleTransport()
    assert transport.connected is True


def test_disconnect_marks_transport_as_disconnected():
    transport = ConsoleTransport()
    transport.disconnect()
    assert transport.connected is False


def test_connect_marks_transport_as_connected():
    transport = ConsoleTransport()
    transport.disconnect()
    transport.connect()
    assert transport.connected is True


def test_connect_is_idempotent():
    transport = ConsoleTransport()
    transport.connect()
    transport.connect()
    assert transport.connected is True


def test_disconnect_is_idempotent():
    transport = ConsoleTransport()
    transport.disconnect()
    transport.disconnect()
    assert transport.connected is False


def test_connect_and_disconnect_print_status(capsys):
    transport = ConsoleTransport()

    transport.disconnect()
    assert "disconnected" in capsys.readouterr().out

    transport.connect()
    assert "connected" in capsys.readouterr().out


# --- send() while disconnected raises ---------------------------------


def test_send_while_disconnected_raises_transport_disconnected_error():
    transport = ConsoleTransport()
    transport.disconnect()

    with pytest.raises(TransportDisconnectedError):
        transport.send('{"messageType": "runtime"}')


def test_transport_disconnected_error_is_a_connection_error():
    assert issubclass(TransportDisconnectedError, ConnectionError)


def test_send_after_reconnecting_succeeds(capsys):
    transport = ConsoleTransport()
    transport.disconnect()
    transport.connect()

    transport.send('{"messageType": "runtime"}')  # should not raise

    assert "runtime" in capsys.readouterr().out


# --- send() while connected: readable stdout output -----------------------


def test_send_prints_to_stdout(capsys):
    transport = ConsoleTransport()

    transport.send('{"messageType": "notification", "payload": {"message": "hi"}}')

    captured = capsys.readouterr()
    assert "[ConsoleTransport]" in captured.out
    assert "notification" in captured.out


def test_send_pretty_prints_valid_json(capsys):
    transport = ConsoleTransport()
    payload = {
        "protocolVersion": 1,
        "messageType": "runtime",
        "timestamp": "2026-09-14T18:00:00Z",
        "payload": {
            "binding": {"deviceId": "birdbox01", "programId": "birdbox", "widgetId": "temperature"},
            "value": 22.5,
        },
    }

    transport.send(json.dumps(payload))

    captured = capsys.readouterr()
    assert _extract_json(captured.out) == payload
    # Pretty-printed, not the single-line compact form.
    assert "\n" in captured.out.strip()


def test_send_falls_back_to_raw_string_for_non_json_data(capsys):
    transport = ConsoleTransport()

    transport.send("not valid json")

    captured = capsys.readouterr()
    assert "not valid json" in captured.out
    assert "[ConsoleTransport] message" in captured.out


def test_send_labels_by_message_type():
    transport = ConsoleTransport()
    import io
    import contextlib

    for message_type in ("registration", "runtime", "notification", "command", "lifecycle"):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            transport.send(json.dumps({"messageType": message_type}))
        assert f"[ConsoleTransport] {message_type}" in buffer.getvalue()
