"""Tests for reconnecting after a lost connection: the backoff policy, the
transport's refusal status and keepalive, the receive loop's hook, and
`Device` re-announcing itself.

The fake transport can drop, refuse and recover on demand, so each scenario
that took a real network fault to produce (a server redeploy, a pulled cable,
a wrong secret) is reproduced here deterministically."""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time

import pytest

from pihub import Device, Program
from pihub.device_config import DeviceConfig, save_device_config
from pihub.device_runtime_service import DeviceRuntimeService
from pihub.protocol.envelope import Envelope
from pihub.protocol.messages import RegistrationMessage, RuntimeMessage, parse_message
from pihub.reconnect import BASE_DELAY_SECONDS, MAX_DELAY_SECONDS, Backoff
from pihub.transport.websocket import (
    DeviceRefusedError,
    TransportConnectionError,
    TransportDisconnectedError,
    WebSocketTransport,
)

_DROP = object()


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class FlakyTransport:
    """A transport that can be made to lose, refuse and regain its connection."""

    def __init__(self) -> None:
        self.connected = False
        self.connections = 0
        self.sent: list = []
        self.connect_failures: list = []
        self._inbox: "queue.Queue" = queue.Queue()
        self._receiving = False

    def connect(self) -> None:
        if self.connect_failures:
            raise self.connect_failures.pop(0)
        self.connected = True
        self.connections += 1

    def disconnect(self) -> None:
        was_connected = self.connected
        self.connected = False
        if self._receiving and was_connected:
            self._inbox.put(_DROP)

    def drop(self) -> None:
        """The network vanishes under a blocked read."""
        self.connected = False
        self._inbox.put(_DROP)

    def send(self, data: str) -> None:
        if not self.connected:
            raise TransportDisconnectedError("disconnected")
        self.sent.append(data)

    def receive(self) -> str:
        if not self.connected:
            raise TransportDisconnectedError("cannot receive: disconnected")
        self._receiving = True
        try:
            item = self._inbox.get(timeout=5)
        finally:
            self._receiving = False
        if item is _DROP:
            raise TransportDisconnectedError("connection lost")
        return item

    def messages(self, kind):
        parsed = [parse_message(Envelope.from_dict(json.loads(raw))) for raw in self.sent]
        return [m for m in parsed if isinstance(m, kind)]


def refused(status=403):
    return TransportConnectionError(f"failed to connect: HTTP {status}", status=status)


def unreachable():
    return TransportConnectionError("failed to connect: connection refused")


def fast(device: Device) -> Device:
    device._backoff = Backoff(base=0.001, cap=0.004, jitter=0)
    return device


@pytest.fixture
def transport():
    return FlakyTransport()


@pytest.fixture
def device(transport, tmp_path):
    made = fast(Device(id="birdbox01", transport=transport, reconnect=True, config_path=tmp_path / "c.json"))
    program = Program(id="birdbox", name="Bird Box")
    made.temperature = program.output(id="temperature", type="Gauge")
    made.status = program.output(id="status", type="Status")
    made.add_program(program)
    yield made
    if made._started:
        made.stop()


# ------------------------------------------------------------------ backoff

def test_backoff_doubles_up_to_a_cap():
    backoff = Backoff(jitter=0)
    delays = [backoff.next_delay() for _ in range(9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    assert (BASE_DELAY_SECONDS, MAX_DELAY_SECONDS) == (1.0, 60.0)


def test_backoff_jitter_stays_within_twenty_percent():
    low, high = Backoff(rng=lambda: 0.0), Backoff(rng=lambda: 1.0)
    assert low.next_delay() == pytest.approx(0.8)
    assert high.next_delay() == pytest.approx(1.2)


def test_backoff_resets():
    backoff = Backoff(jitter=0)
    [backoff.next_delay() for _ in range(5)]
    backoff.reset()
    assert backoff.next_delay() == 1


def test_jitter_spreads_a_fleet_out():
    delays = {round(Backoff().next_delay(), 6) for _ in range(20)}
    assert len(delays) > 5


# ------------------------------------------------------------------ transport

def test_a_refused_handshake_carries_its_status(monkeypatch):
    class Rejected(Exception):
        status_code = 403

    def factory(url, timeout, header=None):
        raise Rejected("Handshake status 403 Forbidden")

    transport = WebSocketTransport("wss://x.test/ws", connection_factory=factory)
    with pytest.raises(TransportConnectionError) as raised:
        transport.connect()
    assert raised.value.status == 403


def test_an_unreachable_server_has_no_status():
    def factory(url, timeout, header=None):
        raise ConnectionRefusedError("refused")

    with pytest.raises(TransportConnectionError) as raised:
        WebSocketTransport("wss://x.test/ws", connection_factory=factory).connect()
    assert raised.value.status is None


class FakeSocket:
    def __init__(self, fail=False):
        self.options, self.fail = [], fail

    def setsockopt(self, level, option, value):
        if self.fail:
            raise OSError("not supported")
        self.options.append((level, option, value))


class ConnectionWithSocket:
    def __init__(self, sock):
        self.sock = sock

    def settimeout(self, timeout): ...
    def send(self, data): ...
    def recv(self): return ""
    def close(self): ...


def test_keepalive_is_enabled_so_a_vanished_peer_is_noticed():
    sock = FakeSocket()
    WebSocketTransport("wss://x.test/ws", connection_factory=lambda *a, **k: ConnectionWithSocket(sock)).connect()

    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in sock.options
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30) in sock.options
        assert (socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3) in sock.options


def test_a_socket_that_rejects_keepalive_still_connects():
    transport = WebSocketTransport(
        "wss://x.test/ws", connection_factory=lambda *a, **k: ConnectionWithSocket(FakeSocket(fail=True))
    )
    transport.connect()
    assert transport.connected


# ------------------------------------------------------------------ the receive loop hook

class ScriptedTransport:
    def __init__(self, script):
        self.script = list(script)

    def receive(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Recorder:
    id = "d"

    def __init__(self):
        self.handled = []

    def handle_incoming(self, data):
        self.handled.append(data)


def run_service(script, on_disconnect):
    recorder, done = Recorder(), threading.Event()
    service = DeviceRuntimeService(recorder, ScriptedTransport(script), on_disconnect=on_disconnect)
    service.start()
    wait_until(lambda: not service.running)
    service.stop()
    return recorder


def test_the_loop_carries_on_after_a_successful_restore():
    calls = []
    recorder = run_service(["a", ConnectionError("lost"), "b", ConnectionError("gone")], lambda stop: calls.append(1) or len(calls) == 1)
    assert recorder.handled == ["a", "b"]
    assert len(calls) == 2


def test_the_loop_ends_when_restoring_gives_up():
    assert run_service([ConnectionError("lost"), "never"], lambda stop: False).handled == []


def test_a_closed_socket_signalled_by_an_empty_frame_also_triggers_a_restore():
    calls = []
    recorder = run_service(["a", "", "b", ""], lambda stop: calls.append(1) or len(calls) == 1)
    assert recorder.handled == ["a", "b"]


def test_without_a_hook_a_lost_connection_still_ends_the_loop():
    assert run_service(["a", ConnectionError("lost"), "never"], None).handled == ["a"]


# ------------------------------------------------------------------ Device: when it reconnects

def _config(tmp_path):
    path = tmp_path / "config.json"
    save_device_config(DeviceConfig("birdbox01", "s3cret", "wss://platform.example.com"), path)
    return path


def test_reconnect_is_on_by_default_for_a_device_that_built_its_own_connection(tmp_path, monkeypatch):
    fake = FlakyTransport()
    monkeypatch.setattr("pihub.transport.websocket.WebSocketTransport", lambda **kwargs: fake)
    device = fast(Device(id="birdbox01", config_path=_config(tmp_path)))

    device.start()
    try:
        assert device._reconnect is True
    finally:
        device.stop()


def test_reconnect_is_off_by_default_when_the_caller_supplied_the_transport(tmp_path):
    fake = FlakyTransport()
    device = Device(id="birdbox01", transport=fake, config_path=tmp_path / "c.json")
    device.start()
    try:
        assert device._reconnect is False
        fake.drop()
        assert wait_until(lambda: not device._runtime_service.running)
    finally:
        device.stop()


def test_reconnect_can_be_switched_off_explicitly(tmp_path, monkeypatch):
    fake = FlakyTransport()
    monkeypatch.setattr("pihub.transport.websocket.WebSocketTransport", lambda **kwargs: fake)
    device = Device(id="birdbox01", config_path=_config(tmp_path), reconnect=False)
    device.start()
    try:
        assert device._reconnect is False
    finally:
        device.stop()


def test_a_transport_with_nothing_to_reconnect_is_left_alone(tmp_path):
    class Console:
        def __init__(self):
            self.sent = []

        def send(self, data):
            self.sent.append(data)

    device = Device(id="birdbox01", transport=Console(), reconnect=True, config_path=tmp_path / "c.json")
    device.start()
    assert device._reconnect is False
    device.stop()


# ------------------------------------------------------------------ Device: recovering

def test_a_dropped_connection_is_restored_and_the_device_re_announces_itself(device, transport):
    device.start()
    assert len(transport.messages(RegistrationMessage)) == 1

    transport.drop()

    assert wait_until(lambda: transport.connections == 2)
    assert wait_until(lambda: len(transport.messages(RegistrationMessage)) == 2)


def test_the_latest_value_of_each_output_is_re_sent_after_a_reconnect(device, transport):
    device.start()
    device.temperature.set(20.0)
    device.temperature.set(22.5)
    device.status.set("online")

    transport.drop()
    assert wait_until(lambda: transport.connections == 2)
    assert wait_until(lambda: len(transport.messages(RuntimeMessage)) >= 5)

    replayed = transport.messages(RuntimeMessage)[3:]
    by_widget = {m.binding.widget_id: m.value for m in replayed}
    assert by_widget == {"temperature": 22.5, "status": "online"}
    assert len(replayed) == 2


def test_a_server_that_stays_down_a_while_is_retried_until_it_returns(device, transport):
    device.start()
    transport.connect_failures = [unreachable(), unreachable(), unreachable()]

    transport.drop()

    assert wait_until(lambda: transport.connections == 2)
    assert transport.connect_failures == []


def test_a_refused_login_while_reconnecting_never_gives_up(device, transport, caplog):
    device.start()
    transport.connect_failures = [refused(403)] * 12

    with caplog.at_level(logging.ERROR, logger="pihub.device"):
        transport.drop()
        assert wait_until(lambda: transport.connections == 2)

    assert "refused this device" in caplog.text and "pihub setup" in caplog.text
    assert "Reactivate" in caplog.text


def test_the_refusal_is_logged_sparingly_not_on_every_attempt(device, transport, caplog):
    device.start()
    transport.connect_failures = [refused(403)] * 12

    with caplog.at_level(logging.ERROR, logger="pihub.device"):
        transport.drop()
        wait_until(lambda: transport.connections == 2)

    assert caplog.text.count("refused this device") == 2  # the 1st and the 10th


def test_sending_during_an_outage_does_not_crash_the_program(device, transport):
    device.start()
    transport.connect_failures = [unreachable()] * 50
    transport.drop()
    assert wait_until(lambda: not transport.connected)

    device.temperature.set(30.0)  # must not raise

    assert device._latest_runtime[("birdbox", "temperature")].value == 30.0


def test_a_value_set_during_the_outage_is_delivered_when_the_connection_returns(device, transport):
    device.start()
    transport.connect_failures = [unreachable()] * 3
    transport.drop()
    wait_until(lambda: not transport.connected)
    device.temperature.set(31.0)

    assert wait_until(lambda: transport.connections == 2)
    assert wait_until(lambda: any(m.value == 31.0 for m in transport.messages(RuntimeMessage)))


def test_without_reconnect_sending_while_disconnected_still_raises(tmp_path):
    transport = FlakyTransport()
    device = Device(id="birdbox01", transport=transport, config_path=tmp_path / "c.json")
    program = Program(id="birdbox", name="Bird Box")
    output = program.output(id="temperature", type="Gauge")
    device.add_program(program)
    device.start()
    transport.drop()
    wait_until(lambda: not transport.connected)

    with pytest.raises(ConnectionError):
        output.set(1.0)
    device.stop()


# ------------------------------------------------------------------ Device: starting

def test_a_platform_that_is_unreachable_at_start_is_not_an_error(device, transport, caplog):
    transport.connect_failures = [unreachable(), unreachable()]

    with caplog.at_level(logging.WARNING, logger="pihub.device"):
        device.start()  # must not raise

    assert "could not reach the platform yet" in caplog.text
    assert wait_until(lambda: transport.connections == 1)
    assert wait_until(lambda: len(transport.messages(RegistrationMessage)) == 1)


def test_a_platform_that_refuses_the_device_at_start_is_reported_immediately(device, transport):
    transport.connect_failures = [refused(403)]

    with pytest.raises(TransportConnectionError) as raised:
        device.start()

    assert raised.value.status == 403


def test_the_refusal_at_start_says_what_to_do_and_hides_the_handshake_traceback(device, transport):
    transport.connect_failures = [refused(403)]

    with pytest.raises(DeviceRefusedError) as raised:
        device.start()

    message = str(raised.value)
    assert "HTTP 403" in message and device.id in message
    assert "Reactivate" in message and "pihub setup" in message  # deactivation first
    assert message.index("Reactivate") < message.index("pihub setup")
    assert "deleted" in message and "account" in message  # a deleted device or account is named too: Reactivate does not help then
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_a_connect_that_succeeds_at_start_announces_once(device, transport):
    device.start()
    assert transport.connections == 1
    assert len(transport.messages(RegistrationMessage)) == 1


# ------------------------------------------------------------------ Device: stopping and pacing

def test_stopping_during_an_outage_returns_promptly_and_leaves_nothing_connected(tmp_path):
    transport = FlakyTransport()
    device = Device(id="birdbox01", transport=transport, reconnect=True, config_path=tmp_path / "c.json")
    device._backoff = Backoff(base=30, cap=30, jitter=0)
    device.start()
    transport.connect_failures = [unreachable()] * 100
    transport.drop()
    wait_until(lambda: not transport.connected)

    started = time.monotonic()
    device.stop()

    assert time.monotonic() - started < 2
    assert not transport.connected


def test_a_stable_connection_resets_the_backoff_but_a_flapping_one_does_not(device):
    clock = {"now": 1000.0}
    device._clock = lambda: clock["now"]
    device._backoff = Backoff(base=1, cap=60, jitter=0)
    for _ in range(4):
        device._backoff.next_delay()  # 1, 2, 4, 8 -> the next would be 16

    device._connected_since = clock["now"] - 5      # flapping: connected for only 5s
    device._note_connection_lost()
    assert device._backoff.next_delay() == 16

    device._connected_since = clock["now"] - 120     # stable: two minutes
    device._note_connection_lost()
    assert device._backoff.next_delay() == 1
    assert device._connected_since is None
