"""Demand-driven uplink (WebRTC_Architecture.md 8.2, Phase 4): the platform gets a device's runtime values only while it needs them.

Not while nobody is watching, and not while every viewer is connected directly. Yes while any viewer is still connecting, failed, or dropped (a relayed viewer, and the
dashboard is never blank while a connection is made), and always when there is no direct path at all.
"""

import json
import os
import threading
import time

import pytest

aiortc = pytest.importorskip("aiortc")

from pihub import Device, Program  # noqa: E402
from pihub.protocol.bindings import Binding  # noqa: E402
from pihub.protocol.messages import RuntimeMessage, SIGNAL_CLOSE, SignalMessage  # noqa: E402
from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402
from pihub.webrtc import peer_manager as pm  # noqa: E402
from tests.webrtc.test_peer_manager import FakeClock, Wire, connect_browser, grant, run, until  # noqa: E402


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def clock():
    return FakeClock()


def manager_with(wire, clock, changes):
    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=3), clock=clock, heartbeat_seconds=0.2, device_id="birdbox01", on_uplink=changes.append)
    peers.start()
    return peers


def put_grant(manager, clock, session_id, state, direct_data=True):
    manager._grants[session_id] = pm._Grant(session_id=session_id, ttl_seconds=900, deadline=clock() + 900, expires_at="x", state=state, direct_data=direct_data)


HELLO = json.dumps({"kind": "hello", "features": ["runtime", "command"]})


# --- the rule ------------------------------------------------------------------------------------------------------------------


def test_a_manager_that_is_not_running_says_the_platform_is_needed(wire, clock):
    peers = PeerManager(wire, DeviceLimits(), clock=clock)
    assert peers.uplink_required() is True  # no direct path exists at all


def test_with_nobody_watching_the_platform_is_not_needed(wire, clock):
    peers = manager_with(wire, clock, [])
    try:
        assert peers.uplink_required() is False
    finally:
        peers.stop()


@pytest.mark.parametrize("state", ["ready", "negotiating", "failed"])
def test_a_viewer_that_is_not_connected_directly_needs_the_platform(wire, clock, state):
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", state)
        assert peers.uplink_required() is True
    finally:
        peers.stop()


def test_a_viewer_connected_directly_does_not_need_the_platform(wire, clock):
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", "connected")
        assert peers.uplink_required() is False
    finally:
        peers.stop()


def test_a_viewer_that_is_connected_but_never_said_it_takes_values_that_way_still_needs_the_platform(wire, clock):
    # An older page (or one still open in a tab from before this release) does not know about direct runtime: it would show a frozen dashboard.
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", "connected", direct_data=False)
        assert peers.uplink_required() is True
    finally:
        peers.stop()


def test_one_viewer_still_connecting_is_enough_to_need_the_platform(wire, clock):
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", "connected")
        put_grant(peers, clock, "s2", "negotiating")
        assert peers.uplink_required() is True
        peers._grants["s2"].state = "connected"
        assert peers.uplink_required() is False
    finally:
        peers.stop()


# --- the callback, on real transitions -------------------------------------------------------------------------------------------


def test_the_device_is_told_when_the_platform_starts_and_stops_being_needed(wire, clock):
    changes = []
    peers = manager_with(wire, clock, changes)
    try:

        async def scenario():
            peers.handle_signal(grant("s1"))
            await until(lambda: changes == [True], what="uplink on when a viewer is granted")
            pc, channel, inbox = await connect_browser(peers, wire, "s1")
            await until(lambda: any('"kind": "hello"' in str(m) for m in inbox), what="the device's hello")
            assert changes == [True]  # connected, but this browser has not said it takes values over the channel yet
            channel.send(HELLO)  # ... now it has
            await until(lambda: changes == [True, False], what="uplink off once connected directly and the browser said so")
            await pc.close()
            await until(lambda: changes == [True, False, True], what="uplink on again when the viewer drops")
            peers.handle_signal(SignalMessage(SIGNAL_CLOSE, "s1", {}))
            return changes

        run(scenario())
    finally:
        peers.stop()


def test_a_session_that_ends_leaves_nothing_needing_the_platform(wire, clock):
    changes = []
    peers = manager_with(wire, clock, changes)
    try:

        async def scenario():
            peers.handle_signal(grant("s1"))
            await until(lambda: changes == [True], what="uplink on")
            peers.handle_signal(SignalMessage(SIGNAL_CLOSE, "s1", {}))
            await until(lambda: changes == [True, False], what="uplink off when the only viewer leaves")

        run(scenario())
    finally:
        peers.stop()


def test_the_device_is_not_told_again_when_nothing_changed(wire, clock):
    changes = []
    peers = manager_with(wire, clock, changes)
    try:
        put_grant(peers, clock, "s1", "ready")
        peers._uplink_check()
        peers._uplink_check()
        peers._grants["s1"].state = "negotiating"  # still not connected: still needed
        peers._uplink_check()
        assert changes == [True]
    finally:
        peers.stop()


def test_a_callback_that_raises_is_contained(wire, clock):
    def boom(required):
        raise RuntimeError("boom")

    peers = PeerManager(wire, DeviceLimits(), clock=clock, on_uplink=boom)
    peers.start()
    try:
        put_grant(peers, clock, "s1", "ready")
        peers._uplink_check()  # must not raise
        assert peers._uplink is True
    finally:
        peers.stop()


# --- the device --------------------------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)

    def runtime_values(self):
        return [json.loads(raw)["payload"]["value"] for raw in self.sent if '"messageType": "runtime"' in raw]


class Viewers:
    """Stands in for the peer manager: says whether the platform is needed."""

    def __init__(self, required):
        self.required = required
        self.published = []

    def uplink_required(self):
        return self.required

    def publish_runtime(self, message):
        self.published.append(message)

    def announce(self):
        pass

    def stop(self):
        pass


def device_with(required):
    transport = Recorder()
    device = Device(id="birdbox01", transport=transport, webrtc=False, device_information=False)
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output("temperature", "Gauge")
    humidity = program.output("humidity", "Gauge")
    device.add_program(program)
    device.start()
    viewers = Viewers(required) if required is not None else None
    device._peers = viewers
    return device, transport, program, temperature, humidity, viewers


def test_a_value_goes_only_to_direct_viewers_when_the_platform_is_not_needed():
    device, transport, _, temperature, _, viewers = device_with(False)
    before = len(transport.sent)
    temperature.set(21.5)

    assert [m.value for m in viewers.published] == [21.5]  # the direct viewers have it
    assert len(transport.sent) == before  # the platform does not
    assert [m.value for m in device._runtime_snapshot()] == [21.5]  # and it is remembered, to be sent when needed
    device._peers = None
    device.stop()


def test_a_value_goes_to_the_platform_as_well_while_it_is_needed():
    device, transport, _, temperature, _, viewers = device_with(True)
    temperature.set(3.0)
    assert transport.runtime_values() == [3.0] and [m.value for m in viewers.published] == [3.0]
    device._peers = None
    device.stop()


def test_a_device_with_no_direct_path_always_sends_to_the_platform():
    device, transport, _, temperature, _, _ = device_with(None)  # no peer manager at all
    temperature.set(9.0)
    assert transport.runtime_values() == [9.0]
    device.stop()


def test_notifications_always_go_up_whatever_the_uplink_says():
    device, transport, program, _, _, _ = device_with(False)
    program.warning("CPU temperature is high")
    assert any('"messageType": "notification"' in raw and "CPU temperature is high" in raw for raw in transport.sent)
    device._peers = None
    device.stop()


def test_the_platform_is_brought_up_to_date_the_moment_it_is_needed():
    device, transport, _, temperature, humidity, _ = device_with(False)
    temperature.set(20.0)
    humidity.set(55)
    assert transport.runtime_values() == []  # nothing yet: nobody needed it

    device._on_uplink(True)
    end = time.monotonic() + 3
    while len(transport.runtime_values()) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert sorted(transport.runtime_values()) == [20.0, 55]
    device._peers = None
    device.stop()


def test_uplink_off_sends_nothing():
    device, transport, _, temperature, _, _ = device_with(False)
    temperature.set(1.0)
    device._on_uplink(False)
    time.sleep(0.1)
    assert transport.runtime_values() == []
    device._peers = None
    device.stop()


def test_the_snapshot_reads_each_value_as_it_sends_so_a_newer_one_is_never_overwritten():
    device, transport, _, temperature, humidity, _ = device_with(False)
    temperature.set(1.0)
    humidity.set(10)

    sent = []
    key = ("birdbox", "humidity")

    def send(data):
        sent.append(json.loads(data)["payload"])
        if len(sent) == 1:  # while the first is going out, a newer humidity is published
            device._latest_runtime[key] = RuntimeMessage(binding=Binding(device_id="birdbox01", program_id="birdbox", widget_id="humidity"), value=99)

    device._transport = type("T", (), {"send": staticmethod(send)})()
    device._send_uplink_snapshot()
    values = {p["binding"]["widgetId"]: p["value"] for p in sent}
    assert values["humidity"] == 99  # the newest, not the value it held when the snapshot began
    device._peers = None
    device.stop()


def test_a_snapshot_that_cannot_be_sent_does_not_raise():
    device, _, _, temperature, _, _ = device_with(False)
    temperature.set(1.0)

    class Down:
        def send(self, data):
            raise ConnectionError("platform unreachable")

    device._transport = Down()
    device._send_uplink_snapshot()  # contained: the reconnect's replay brings the platform up to date
    device._peers = None


def test_after_a_reconnect_the_latest_values_are_replayed_only_if_the_platform_needs_them():
    device, transport, _, temperature, _, viewers = device_with(False)
    temperature.set(7.0)
    transport.sent.clear()

    device._announce()  # what a reconnect does
    assert transport.runtime_values() == []  # nobody needs them
    assert any('"messageType": "registration"' in raw or '"messageType": "manifest"' in raw or "programs" in raw for raw in transport.sent)  # but it re-introduces itself

    viewers.required = True
    transport.sent.clear()
    device._announce()
    assert transport.runtime_values() == [7.0]
    device._peers = None
    device.stop()


# --- the handshake ------------------------------------------------------------------------------------------------------------------


def test_a_device_says_what_it_can_do_as_soon_as_a_channel_opens_and_before_its_snapshot(wire, clock):
    latest = [pm.RuntimeMessage(binding=Binding(device_id="birdbox01", program_id="birdbox", widget_id="temperature"), value=21.0)]
    peers = PeerManager(wire, DeviceLimits(), clock=clock, heartbeat_seconds=0.2, device_id="birdbox01", snapshot=lambda: latest)
    peers.start()
    try:

        async def scenario():
            peers.handle_signal(grant())
            _, _, inbox = await connect_browser(peers, wire)
            await until(lambda: any('"kind": "runtime"' in str(m) for m in inbox), what="the snapshot")
            return [json.loads(m)["kind"] for m in inbox if isinstance(m, str) and m.startswith("{")]

        kinds = run(scenario())
    finally:
        peers.stop()
    assert kinds.index("hello") < kinds.index("runtime")
    assert pm.DIRECT_FEATURES == ["runtime", "command"]


def test_a_browsers_hello_is_what_moves_a_viewer_off_the_platforms_path(wire, clock):
    changes = []
    peers = manager_with(wire, clock, changes)
    try:
        put_grant(peers, clock, "s1", "connected", direct_data=False)
        peers._uplink_check()  # connected, but this browser has said nothing: the platform is needed
        peers._on_channel_message(peers._grants["s1"], HELLO)
        assert peers._grants["s1"].direct_data is True and peers.uplink_required() is False
        assert changes == [True, False]  # ... and stops being needed once it has spoken
    finally:
        peers.stop()


@pytest.mark.parametrize(
    "features", ['"runtime"', "[]", '["command"]', "null", "7"], ids=["not a list", "empty", "commands only", "null", "a number"]
)
def test_a_hello_that_does_not_include_runtime_does_not_move_the_viewer(wire, clock, features):
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", "connected", direct_data=False)
        peers._on_channel_message(peers._grants["s1"], '{"kind": "hello", "features": %s}' % features)
        assert peers._grants["s1"].direct_data is False and peers.uplink_required() is True
    finally:
        peers.stop()


def test_a_new_channel_must_say_hello_again(wire, clock):
    peers = manager_with(wire, clock, [])
    try:
        put_grant(peers, clock, "s1", "connected", direct_data=True)

        class Channel:
            readyState = "connecting"

            def on(self, name, handler=None):
                return lambda f: f

        peers._attach_channel(peers._grants["s1"], Channel())
        assert peers._grants["s1"].direct_data is False  # the old channel's word does not carry over
    finally:
        peers.stop()


def test_a_browser_that_takes_it_back_puts_the_platform_back_in_the_loop(wire, clock):
    changes = []
    peers = manager_with(wire, clock, changes)
    try:
        put_grant(peers, clock, "s1", "connected", direct_data=False)
        peers._uplink_check()
        peers._on_channel_message(peers._grants["s1"], HELLO)
        peers._on_channel_message(peers._grants["s1"], '{"kind": "hello", "features": []}')
        assert changes == [True, False, True]
    finally:
        peers.stop()

