import json
import queue
import threading
import time

from pihub import Device, Program, WebSocketTransport
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import (
    CommandMessage,
    LifecycleMessage,
    NotificationMessage,
    RegistrationMessage,
    RuntimeMessage,
)


class FakeConnection:
    """Stands in for a real socket, including blocking-receive semantics.

    Once `Device.start()` spins up a background `DeviceRuntimeService`
    (see device_runtime_service.py), something is *always* calling
    `recv()` in a loop the moment the connection is established - so
    unlike a plain list, `recv()` here must actually block until either a
    message arrives or the connection is closed, exactly like a real
    blocking socket read would. `queue.Queue.get(timeout=...)` polling is
    what makes `close()` from another thread able to interrupt a `recv()`
    that's already in progress.
    """

    def __init__(self) -> None:
        self.sent: list = []
        self.inbox: "queue.Queue[str]" = queue.Queue()
        self._closed = threading.Event()

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str:
        while not self._closed.is_set():
            try:
                return self.inbox.get(timeout=0.05)
            except queue.Empty:
                continue
        raise ConnectionError("connection closed")

    def settimeout(self, timeout) -> None:
        pass  # this fake's blocking semantics don't depend on a real socket timeout

    def close(self) -> None:
        self._closed.set()


class FakeFactory:
    def __init__(self) -> None:
        self.connections: list = []

    def __call__(self, url: str, timeout: float, header=None) -> FakeConnection:
        connection = FakeConnection()
        self.connections.append(connection)
        return connection


def make_transport() -> tuple[WebSocketTransport, FakeFactory]:
    factory = FakeFactory()
    transport = WebSocketTransport(url="wss://example.test/devices", connection_factory=factory)
    return transport, factory


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


def test_device_lifecycle_over_websocket_transport():
    transport, factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")
    temperature = birdbox.output(id="temperature", type="Gauge")
    device.add_program(birdbox)

    transport.connect()
    device.start()
    temperature.set(22.5)
    device.stop()

    sent = factory.connections[0].sent
    assert len(sent) == 2

    registration = RegistrationMessage.from_payload(json.loads(sent[0])["payload"])
    assert registration.device_id == "birdbox01"

    runtime = RuntimeMessage.from_payload(json.loads(sent[1])["payload"])
    assert runtime.binding.canonical_id == "birdbox01.birdbox.temperature"
    assert runtime.value == 22.5


def test_device_start_connects_the_transport_automatically():
    """`device.start()` alone (no prior manual
    `transport.connect()`) must connect the transport."""
    transport, _factory = make_transport()
    device = Device(id="birdbox01", transport=transport)

    device.start()

    assert transport.connected is True
    device.stop()


def test_notification_sent_over_websocket_after_reconnect():
    transport, factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")
    device.add_program(birdbox)

    transport.connect()
    device.start()
    transport.disconnect()
    transport.reconnect()

    birdbox.info("Snapshot captured")
    device.stop()

    sent = factory.connections[-1].sent
    notification = NotificationMessage.from_payload(json.loads(sent[0])["payload"])
    assert notification.message == "Snapshot captured"


def test_background_receive_loop_dispatches_an_inbound_command_automatically():
    """The device-side half of the Command Relay, now fully automatic: a
    `CommandMessage` pushed onto the connection is picked up by the
    background `DeviceRuntimeService` and dispatched to the real handler -
    no application code calls `transport.receive()` or
    `device.handle_incoming()` itself."""
    transport, factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")
    snapshot = birdbox.command(id="snapshot", type="Button")
    received = []

    @snapshot.handler
    def take_snapshot(command):
        received.append(command)

    device.add_program(birdbox)
    device.start()

    binding = Binding(device_id="birdbox01", program_id="birdbox", widget_id="snapshot")
    pushed = CommandMessage(binding=binding, action="press")
    factory.connections[0].inbox.put(pushed.to_json())

    _wait_until(lambda: len(received) == 1)
    assert received[0].action == "press"

    device.stop()


def test_background_receive_loop_dispatches_an_inbound_lifecycle_event_automatically():
    transport, factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    birdbox = Program(id="birdbox", name="Bird Box")
    events = []

    @birdbox.on_first_user_connected
    def start_camera(payload):
        events.append(payload)

    device.add_program(birdbox)
    device.start()

    pushed = LifecycleMessage.first_user_connected()
    factory.connections[0].inbox.put(pushed.to_json())

    _wait_until(lambda: len(events) == 1)

    device.stop()


def test_stop_ends_the_background_receive_loop():
    transport, factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    device.start()

    assert device._runtime_service.running is True

    device.stop()

    assert device._runtime_service.running is False
    assert transport.connected is False


def test_stop_is_safe_to_call_more_than_once():
    transport, _factory = make_transport()
    device = Device(id="birdbox01", transport=transport)
    device.start()

    device.stop()
    device.stop()  # should not raise
