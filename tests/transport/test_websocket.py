import pytest

from pihub.device import Transport
from pihub.transport.websocket import (
    ConnectionState,
    TransportConnectionError,
    TransportDisconnectedError,
    WebSocketTransport,
)


class FakeConnection:
    """Stand-in for the object `websocket.create_connection()` returns."""

    def __init__(self) -> None:
        self.sent: list = []
        self.closed = False
        self.fail_send = False
        self.fail_recv = False
        self.inbox: list = []
        self.timeout: object = "unset"

    def send(self, data: str) -> None:
        if self.fail_send:
            raise ConnectionError("socket write failed")
        self.sent.append(data)

    def recv(self) -> str:
        if self.fail_recv:
            raise ConnectionError("socket read failed")
        return self.inbox.pop(0)

    def close(self) -> None:
        self.closed = True

    def settimeout(self, timeout) -> None:
        self.timeout = timeout


class FakeFactory:
    """A `connection_factory` that records calls and can be told to fail."""

    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list = []
        self.connections: list = []

    def __call__(self, url: str, timeout: float, header=None) -> FakeConnection:
        self.calls.append((url, timeout))
        if len(self.calls) <= self.fail_times:
            raise ConnectionRefusedError("connection refused")
        connection = FakeConnection()
        self.connections.append(connection)
        return connection


def make_transport(**overrides) -> WebSocketTransport:
    kwargs = dict(
        url="wss://example.test/devices",
        connect_timeout=5.0,
        max_reconnect_attempts=3,
        reconnect_delay=0,
        reconnect_backoff=1,
        connection_factory=FakeFactory(),
        sleep=lambda _seconds: None,
    )
    kwargs.update(overrides)
    return WebSocketTransport(**kwargs)


# --- extends the existing Transport base class -------------------------


def test_websocket_transport_is_a_transport_subclass():
    assert issubclass(WebSocketTransport, Transport)


def test_websocket_transport_instance_satisfies_transport_protocol():
    assert isinstance(make_transport(), Transport)


# --- initial state -------------------------------------------------------


def test_starts_disconnected():
    transport = make_transport()
    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED


# --- connect() -------------------------------------------------------------


def test_connect_marks_transport_as_connected():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)

    transport.connect()

    assert transport.connected is True
    assert transport.state == ConnectionState.CONNECTED
    assert factory.calls == [("wss://example.test/devices", 5.0)]


def test_connect_clears_the_socket_timeout_after_connecting():
    """`connect_timeout` bounds the connection attempt only - once
    connected, the socket must not keep that timeout for every future
    `send()`/`receive()` call, or an idle-but-healthy connection would
    look indistinguishable from a dead one after `connect_timeout`
    seconds of silence."""
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory, connect_timeout=5.0)

    transport.connect()

    assert factory.connections[0].timeout is None


def test_connect_failure_during_settimeout_leaves_transport_disconnected():
    """A connection object that connects fine but rejects `settimeout()`
    (some real socket types can, in principle) must still be treated as a
    failed connect - not left half-configured."""

    def failing_factory(url, timeout):
        connection = FakeConnection()

        def raise_on_settimeout(_timeout):
            raise OSError("settimeout not supported")

        connection.settimeout = raise_on_settimeout
        return connection

    transport = make_transport(connection_factory=failing_factory)

    with pytest.raises(TransportConnectionError):
        transport.connect()

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED


def test_connect_is_idempotent_when_already_connected():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)

    transport.connect()
    transport.connect()

    assert transport.connected is True
    assert len(factory.calls) == 1  # second connect() was a no-op


def test_connect_failure_raises_and_leaves_transport_disconnected():
    factory = FakeFactory(fail_times=1)
    transport = make_transport(connection_factory=factory)

    with pytest.raises(TransportConnectionError):
        transport.connect()

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED


# --- disconnect() ----------------------------------------------------------


def test_disconnect_closes_the_connection_and_updates_state():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()

    transport.disconnect()

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED
    assert factory.connections[0].closed is True


def test_disconnect_is_idempotent():
    transport = make_transport()

    transport.disconnect()
    transport.disconnect()

    assert transport.connected is False


def test_disconnect_tolerates_close_errors():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    factory.connections[0].close = lambda: (_ for _ in ()).throw(ConnectionError("already gone"))

    transport.disconnect()  # should not raise

    assert transport.connected is False


# --- send() ------------------------------------------------------------


def test_send_while_disconnected_raises_transport_disconnected_error():
    transport = make_transport()

    with pytest.raises(TransportDisconnectedError):
        transport.send('{"messageType": "runtime"}')


def test_transport_disconnected_error_is_a_connection_error():
    assert issubclass(TransportDisconnectedError, ConnectionError)


def test_transport_connection_error_is_a_connection_error():
    assert issubclass(TransportConnectionError, ConnectionError)


def test_send_forwards_data_to_the_connection_when_connected():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()

    transport.send('{"messageType": "runtime"}')

    assert factory.connections[0].sent == ['{"messageType": "runtime"}']


def test_send_after_connection_drops_raises_and_marks_disconnected():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    factory.connections[0].fail_send = True

    with pytest.raises(TransportDisconnectedError):
        transport.send('{"messageType": "runtime"}')

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED


def test_send_after_reconnecting_succeeds():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    transport.disconnect()
    transport.connect()

    transport.send('{"messageType": "runtime"}')  # should not raise

    assert factory.connections[-1].sent == ['{"messageType": "runtime"}']


# --- receive() -----------------------------------------------------------


def test_receive_while_disconnected_raises_transport_disconnected_error():
    transport = make_transport()

    with pytest.raises(TransportDisconnectedError):
        transport.receive()


def test_receive_returns_the_next_message_from_the_connection():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    factory.connections[0].inbox.append('{"messageType": "command"}')

    message = transport.receive()

    assert message == '{"messageType": "command"}'


def test_receive_returns_messages_in_order():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    factory.connections[0].inbox.extend(["first", "second"])

    assert transport.receive() == "first"
    assert transport.receive() == "second"


def test_receive_after_connection_drops_raises_and_marks_disconnected():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    factory.connections[0].fail_recv = True

    with pytest.raises(TransportDisconnectedError):
        transport.receive()

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED


# --- reconnect() -------------------------------------------------------


def test_reconnect_succeeds_after_transient_failures():
    factory = FakeFactory(fail_times=2)
    transport = make_transport(connection_factory=factory, max_reconnect_attempts=3)

    transport.reconnect()

    assert transport.connected is True
    assert transport.state == ConnectionState.CONNECTED
    assert len(factory.calls) == 3


def test_reconnect_gives_up_after_max_attempts_and_raises():
    factory = FakeFactory(fail_times=10)
    transport = make_transport(connection_factory=factory, max_reconnect_attempts=3)

    with pytest.raises(TransportConnectionError):
        transport.reconnect()

    assert transport.connected is False
    assert transport.state == ConnectionState.DISCONNECTED
    assert len(factory.calls) == 3


def test_reconnect_closes_the_existing_connection_first():
    factory = FakeFactory()
    transport = make_transport(connection_factory=factory)
    transport.connect()
    first_connection = factory.connections[0]

    transport.reconnect()

    assert first_connection.closed is True
    assert transport.connected is True
    assert factory.connections[-1] is not first_connection


def test_reconnect_sleeps_with_backoff_between_attempts():
    sleeps = []
    factory = FakeFactory(fail_times=2)
    transport = make_transport(
        connection_factory=factory,
        max_reconnect_attempts=3,
        reconnect_delay=1,
        reconnect_backoff=2,
        sleep=sleeps.append,
    )

    transport.reconnect()

    assert sleeps == [1, 2]
