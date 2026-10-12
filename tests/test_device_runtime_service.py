"""Unit tests for `DeviceRuntimeService`: its error handling and its threading model.

`tests/transport/test_websocket_integration.py` already proves this
wired up behind a real `Device` + `WebSocketTransport`; these tests
exercise the service in isolation, against small fakes, so every branch
(disconnect, unexpected receive error, unexpected handler error) is
covered deterministically and fast.

`FakeTransport.close()` mirrors what `Device.stop()` actually does to a
real transport before stopping the service (see device.py's `stop()`
docstring): a bare stop signal can't interrupt an in-flight blocking
`receive()` call, only closing the underlying connection can. Tests that
don't care about that call `close()` before `stop()` too, to keep
teardown fast and accurate rather than waiting out `stop()`'s timeout.
"""

from __future__ import annotations

import queue
import threading
import time

from pihub.device_runtime_service import DeviceRuntimeService


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


class FakeDevice:
    def __init__(self, id: str = "birdbox01") -> None:
        self.id = id
        self.handled: list = []
        self.fail_next_handle = False

    def handle_incoming(self, data: str) -> None:
        if self.fail_next_handle:
            self.fail_next_handle = False
            raise ValueError("boom handling")
        self.handled.append(data)


class FakeTransport:
    """A minimal `ReceivingTransport`: a pollable, closeable queue.

    Polls rather than blocking forever on an empty queue, so a
    `queue_error()` or `close()` call made *after* a `receive()` call is
    already in flight still takes effect on the next poll, instead of
    racing a blocked `get()`.
    """

    def __init__(self) -> None:
        self.inbox: "queue.Queue[str]" = queue.Queue()
        self._raise_once: list = []
        self._closed = threading.Event()

    def queue_error(self, exc: Exception) -> None:
        self._raise_once.append(exc)

    def close(self) -> None:
        self._closed.set()

    def receive(self) -> str:
        while not self._closed.is_set():
            if self._raise_once:
                raise self._raise_once.pop(0)
            try:
                return self.inbox.get(timeout=0.02)
            except queue.Empty:
                continue
        raise ConnectionError("transport closed")


def test_running_is_false_before_start():
    service = DeviceRuntimeService(FakeDevice(), FakeTransport())
    assert service.running is False


def test_start_makes_the_service_running():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)

    service.start()

    assert service.running is True
    transport.close()
    service.stop()


def test_start_is_idempotent():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)

    service.start()
    first_thread = service._thread
    service.start()  # should not spawn a second thread

    assert service._thread is first_thread
    transport.close()
    service.stop()


def test_stop_before_start_is_safe():
    service = DeviceRuntimeService(FakeDevice(), FakeTransport())
    service.stop()  # should not raise
    assert service.running is False


def test_stop_is_safe_to_call_more_than_once():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    transport.close()
    service.stop()
    service.stop()  # should not raise

    assert service.running is False


def test_received_messages_are_passed_to_handle_incoming():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)

    service.start()
    transport.inbox.put("hello")
    transport.inbox.put("world")

    _wait_until(lambda: device.handled == ["hello", "world"])

    transport.close()
    service.stop()


def test_connection_error_stops_the_loop_cleanly():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    transport.queue_error(ConnectionError("disconnected"))

    _wait_until(lambda: service.running is False)

    # stop() afterwards is still safe even though the thread already exited.
    service.stop()


def test_a_connection_error_subclass_also_stops_the_loop():
    """`TransportDisconnectedError` (both transport modules' variant) is a
    `ConnectionError` subclass - this proves the catch is by base class,
    not a hardcoded specific exception type."""

    class CustomTransportDisconnectedError(ConnectionError):
        pass

    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    transport.queue_error(CustomTransportDisconnectedError("gone"))

    _wait_until(lambda: service.running is False)
    service.stop()


def test_unexpected_receive_error_is_logged_and_the_loop_continues():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    transport.queue_error(ValueError("transient glitch"))
    transport.inbox.put("still works")

    _wait_until(lambda: device.handled == ["still works"])
    assert service.running is True

    transport.close()
    service.stop()


def test_unexpected_handle_incoming_error_is_logged_and_the_loop_continues():
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    device.fail_next_handle = True
    transport.inbox.put("this one blows up in handle_incoming")
    transport.inbox.put("this one should still arrive")

    _wait_until(lambda: device.handled == ["this one should still arrive"])
    assert service.running is True

    transport.close()
    service.stop()


def test_stop_join_returns_promptly_once_the_thread_has_exited():
    """Regression guard: `stop()` must not block for its full timeout when
    the loop has already exited on its own (e.g. after a disconnect)."""
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()
    transport.queue_error(ConnectionError("disconnected"))
    _wait_until(lambda: service.running is False)

    start = time.monotonic()
    service.stop(timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0


def test_stop_closes_promptly_when_transport_is_closed_first():
    """Mirrors `Device.stop()`'s actual order: closing the transport
    first is what makes `stop()` return quickly, not the stop signal
    alone (see device.py's `stop()` docstring)."""
    device, transport = FakeDevice(), FakeTransport()
    service = DeviceRuntimeService(device, transport)
    service.start()

    transport.close()

    start = time.monotonic()
    service.stop(timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0
    assert service.running is False


def test_an_empty_frame_ends_the_loop_cleanly():
    """A closed socket surfaces as `''` from `websocket-client`'s `recv()`,
    not as an exception. Treated as data it becomes a JSON error, logged
    and retried immediately against the same dead socket - a hot loop
    spewing tracebacks. Found by `scripts/misbehaving_device.py`.
    """

    class ClosingTransport:
        def __init__(self) -> None:
            self.calls = 0

        def receive(self) -> str:
            self.calls += 1
            return ""

    class RecordingDevice:
        id = "birdbox01"

        def __init__(self) -> None:
            self.handled = []

        def handle_incoming(self, data: str) -> None:
            self.handled.append(data)

    transport = ClosingTransport()
    device = RecordingDevice()
    service = DeviceRuntimeService(device, transport)

    service.start()
    service.stop(timeout=2)

    assert transport.calls == 1  # not a hot loop
    assert device.handled == []  # an empty frame is never dispatched
