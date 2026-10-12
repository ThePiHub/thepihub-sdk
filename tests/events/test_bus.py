import logging
import threading

import pytest

from pihub.events.bus import EventBus


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


class Recorder:
    """Collects handler invocations for assertions."""

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, payload) -> None:
        self.calls.append(payload)


def test_subscribe_and_publish_delivers_payload(bus):
    recorder = Recorder()
    bus.subscribe("command.snapshot", recorder)

    bus.publish("command.snapshot", {"action": "press"})

    assert recorder.calls == [{"action": "press"}]


def test_publish_with_no_payload_delivers_none(bus):
    recorder = Recorder()
    bus.subscribe("lifecycle.firstUserConnected", recorder)

    bus.publish("lifecycle.firstUserConnected")

    assert recorder.calls == [None]


def test_publish_with_no_subscribers_does_nothing(bus):
    # Should not raise.
    bus.publish("notification.warning", {"message": "Storage 90% full"})


def test_multiple_handlers_invoked_in_subscription_order(bus):
    order = []
    bus.subscribe("system.tick", lambda payload: order.append("first"))
    bus.subscribe("system.tick", lambda payload: order.append("second"))
    bus.subscribe("system.tick", lambda payload: order.append("third"))

    bus.publish("system.tick")

    assert order == ["first", "second", "third"]


def test_events_are_isolated_by_name(bus):
    snapshot_recorder = Recorder()
    alarm_recorder = Recorder()
    bus.subscribe("command.snapshot", snapshot_recorder)
    bus.subscribe("command.alarm", alarm_recorder)

    bus.publish("command.snapshot", "pressed")

    assert snapshot_recorder.calls == ["pressed"]
    assert alarm_recorder.calls == []


def test_unsubscribe_stops_future_delivery(bus):
    recorder = Recorder()
    bus.subscribe("command.snapshot", recorder)
    bus.unsubscribe("command.snapshot", recorder)

    bus.publish("command.snapshot", "pressed")

    assert recorder.calls == []


def test_unsubscribe_unknown_event_is_a_noop(bus):
    recorder = Recorder()

    # Should not raise even though "command.snapshot" was never subscribed to.
    bus.unsubscribe("command.snapshot", recorder)


def test_unsubscribe_unknown_handler_is_a_noop(bus):
    subscribed = Recorder()
    not_subscribed = Recorder()
    bus.subscribe("command.snapshot", subscribed)

    # Should not raise even though `not_subscribed` was never added.
    bus.unsubscribe("command.snapshot", not_subscribed)

    bus.publish("command.snapshot", "pressed")
    assert subscribed.calls == ["pressed"]


def test_duplicate_subscription_invokes_handler_once_per_subscription(bus):
    recorder = Recorder()
    bus.subscribe("command.snapshot", recorder)
    bus.subscribe("command.snapshot", recorder)

    bus.publish("command.snapshot", "pressed")

    assert recorder.calls == ["pressed", "pressed"]


def test_unsubscribe_removes_only_one_of_duplicate_subscriptions(bus):
    recorder = Recorder()
    bus.subscribe("command.snapshot", recorder)
    bus.subscribe("command.snapshot", recorder)

    bus.unsubscribe("command.snapshot", recorder)
    bus.publish("command.snapshot", "pressed")

    assert recorder.calls == ["pressed"]


def test_subscribe_rejects_non_callable(bus):
    with pytest.raises(TypeError):
        bus.subscribe("command.snapshot", "not callable")


def test_handler_exception_is_logged_and_does_not_stop_dispatch(bus, caplog):
    remaining = Recorder()

    def failing_handler(payload):
        raise ValueError("boom")

    bus.subscribe("notification.error", failing_handler)
    bus.subscribe("notification.error", remaining)

    with caplog.at_level(logging.ERROR, logger="pihub.events.bus"):
        bus.publish("notification.error", "Camera disconnected")

    assert remaining.calls == ["Camera disconnected"]
    assert "boom" in caplog.text or "ValueError" in caplog.text


def test_publish_does_not_raise_when_handler_raises(bus):
    def failing_handler(payload):
        raise RuntimeError("should be swallowed")

    bus.subscribe("notification.error", failing_handler)

    # Should not raise despite the handler failing.
    bus.publish("notification.error", "oops")


def test_handler_can_unsubscribe_itself_during_publish(bus):
    calls = []

    def self_removing_handler(payload):
        calls.append(payload)
        bus.unsubscribe("system.tick", self_removing_handler)

    bus.subscribe("system.tick", self_removing_handler)

    bus.publish("system.tick", 1)
    bus.publish("system.tick", 2)

    assert calls == [1]


def test_handler_can_subscribe_new_handler_during_publish(bus):
    late_recorder = Recorder()

    def subscribing_handler(payload):
        bus.subscribe("system.tick", late_recorder)

    bus.subscribe("system.tick", subscribing_handler)

    bus.publish("system.tick", "first")
    assert late_recorder.calls == []

    bus.publish("system.tick", "second")
    assert late_recorder.calls == ["second"]


def test_unsubscribe_all_handlers_cleans_up_event_entry(bus):
    recorder = Recorder()
    bus.subscribe("command.snapshot", recorder)
    bus.unsubscribe("command.snapshot", recorder)

    with bus._lock:
        assert "command.snapshot" not in bus._handlers


def test_thread_safe_concurrent_subscribe_and_publish():
    bus = EventBus()
    handler_count = 50
    publishes_per_thread = 100
    lock = threading.Lock()
    total_calls = {"count": 0}

    def make_handler():
        def handler(payload):
            with lock:
                total_calls["count"] += 1

        return handler

    handlers = [make_handler() for _ in range(handler_count)]
    for handler in handlers:
        bus.subscribe("system.tick", handler)

    errors = []

    def publisher():
        try:
            for _ in range(publishes_per_thread):
                bus.publish("system.tick", "tick")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=publisher) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert total_calls["count"] == handler_count * publishes_per_thread * 8


def test_thread_safe_concurrent_subscribe_unsubscribe_publish():
    bus = EventBus()
    errors = []

    def subscriber():
        try:
            for _ in range(200):
                handler = lambda payload: None
                bus.subscribe("system.tick", handler)
                bus.unsubscribe("system.tick", handler)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def publisher():
        try:
            for _ in range(200):
                bus.publish("system.tick", "tick")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=subscriber) for _ in range(4)]
    threads += [threading.Thread(target=publisher) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
