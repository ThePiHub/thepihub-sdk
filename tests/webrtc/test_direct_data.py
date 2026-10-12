"""Runtime values and commands over the DataChannel (WebRTC_Architecture.md Phases 2 and 3): what the device sends a directly connected viewer, and what it
accepts back.

The validation, the rate limit and the expiry are tested against a fake channel (deterministic); the wiring is tested with a real WebRTC peer.
"""

import asyncio
import json
import queue
import time

import pytest

aiortc = pytest.importorskip("aiortc")

from pihub.protocol.bindings import Binding  # noqa: E402
from pihub.protocol.messages import CommandMessage, RuntimeMessage  # noqa: E402
from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402
from pihub.webrtc import peer_manager as pm  # noqa: E402
from tests.webrtc.test_peer_manager import FakeClock, Wire, connect_browser, grant, run, until  # noqa: E402


def runtime(widget="temperature", value=22.5, program="birdbox", device="birdbox01"):
    return RuntimeMessage(binding=Binding(device_id=device, program_id=program, widget_id=widget), value=value)


def frames(inbox, kind):
    out = []
    for raw in list(inbox):
        try:
            body = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and body.get("kind") == kind:
            out.append(body)
    return out


class Handler:
    """Stands in for `Device.handle_incoming`."""

    def __init__(self):
        self.received = []

    def __call__(self, text):
        self.received.append(json.loads(text))


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def clock():
    return FakeClock()


def make(wire, clock, **kwargs):
    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=2), clock=clock, heartbeat_seconds=0.2, device_id="birdbox01", **kwargs)
    peers.start()
    return peers


# --- through a real WebRTC peer ------------------------------------------------------------------------------------------------


def test_a_published_value_reaches_a_connected_viewer_with_the_same_fields_as_a_platform_push(wire, clock):
    manager = make(wire, clock)
    try:

        async def scenario():
            manager.handle_signal(grant())
            _, _, inbox = await connect_browser(manager, wire)
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            manager.publish_runtime(runtime(value=23.5))
            await until(lambda: frames(inbox, "runtime"), what="the runtime frame")
            return frames(inbox, "runtime")[0]

        frame = run(scenario())
    finally:
        manager.stop()
    assert frame["binding"] == {"deviceId": "birdbox01", "programId": "birdbox", "widgetId": "temperature"}
    assert frame["value"] == 23.5 and abs(frame["timestamp"] - time.time()) < 60


def test_a_multi_value_update_is_sent_as_values(wire, clock):
    manager = make(wire, clock)
    try:

        async def scenario():
            manager.handle_signal(grant())
            _, _, inbox = await connect_browser(manager, wire)
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            manager.publish_runtime(RuntimeMessage(binding=Binding(device_id="birdbox01", program_id="birdbox", widget_id="weather"), values={"t": 1, "h": 2}))
            await until(lambda: frames(inbox, "runtime"), what="the runtime frame")
            return frames(inbox, "runtime")[0]

        frame = run(scenario())
    finally:
        manager.stop()
    assert frame["values"] == {"t": 1, "h": 2} and "value" not in frame


def test_a_viewer_that_has_just_connected_is_sent_the_latest_of_everything(wire, clock):
    latest = [runtime("temperature", 21.0), runtime("humidity", 55)]
    manager = make(wire, clock, snapshot=lambda: latest)
    try:

        async def scenario():
            manager.handle_signal(grant())
            _, _, inbox = await connect_browser(manager, wire)
            await until(lambda: len(frames(inbox, "runtime")) >= 2, what="the snapshot")
            return frames(inbox, "runtime")

        got = run(scenario())
    finally:
        manager.stop()
    assert {(f["binding"]["widgetId"], f["value"]) for f in got} == {("temperature", 21.0), ("humidity", 55)}


def test_every_connected_viewer_gets_each_value(wire, clock):
    manager = make(wire, clock)
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            manager.handle_signal(grant("s2"))
            _, _, first = await connect_browser(manager, wire, "s1")
            _, _, second = await connect_browser(manager, wire, "s2")
            await until(lambda: frames(first, "heartbeat") and frames(second, "heartbeat"), what="both channels")
            manager.publish_runtime(runtime(value=5))
            await until(lambda: frames(first, "runtime") and frames(second, "runtime"), what="both frames")

        run(scenario())
    finally:
        manager.stop()


def test_a_value_published_with_nobody_connected_or_before_the_channel_is_open_costs_nothing(wire, clock):
    manager = make(wire, clock)
    try:
        manager.publish_runtime(runtime())  # no grants at all
        manager.handle_signal(grant())
        manager.publish_runtime(runtime())  # a grant, no channel yet
        time.sleep(0.1)
        assert manager.session_ids() == ["s1"]
    finally:
        manager.stop()


def test_a_command_sent_over_the_channel_reaches_the_device_and_is_acknowledged(wire, clock):
    handler = Handler()
    manager = make(wire, clock, on_command=handler)
    try:

        async def scenario():
            manager.handle_signal(grant())
            _, channel, inbox = await connect_browser(manager, wire)
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            channel.send(json.dumps({"kind": "command", "id": "c1", "programId": "device-information", "widgetId": "camera-frame-rate", "values": {"value": 15}}))
            await until(lambda: frames(inbox, "command-result"), what="the acknowledgement")
            await until(lambda: handler.received, what="the device to receive it")
            return frames(inbox, "command-result")[0]

        result = run(scenario())
    finally:
        manager.stop()
    assert result == {"kind": "command-result", "id": "c1", "status": 202}
    message = CommandMessage.from_payload(handler.received[0]["payload"])
    assert (message.binding.device_id, message.binding.program_id, message.binding.widget_id) == ("birdbox01", "device-information", "camera-frame-rate")
    assert message.values == {"value": 15}


# --- against a fake channel: what is refused, and why ---------------------------------------------------------------------------


class FakeChannel:
    readyState = "open"

    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(json.loads(text))


def open_grant(manager, clock, ttl=900):
    manager._grants["s1"] = pm._Grant(session_id="s1", ttl_seconds=ttl, deadline=clock() + ttl, expires_at="x", channel=FakeChannel())
    return manager._grants["s1"]


def send_command(manager, grant_, body, size=100):
    manager._on_command_frame(grant_, body, size)
    return grant_.channel.sent[-1]


GOOD = {"kind": "command", "id": 7, "programId": "birdbox", "widgetId": "snapshot", "action": "press"}


@pytest.fixture
def unit(wire, clock):
    handler = Handler()
    manager = PeerManager(wire, DeviceLimits(), clock=clock, device_id="birdbox01", on_command=handler)  # not started: single threaded, deterministic
    return manager, handler, open_grant(manager, clock), clock


def test_a_good_command_is_queued_for_the_devices_handlers_not_run_on_the_connection(unit):
    manager, handler, grant_, _ = unit
    assert send_command(manager, grant_, GOOD) == {"kind": "command-result", "id": 7, "status": 202}
    assert handler.received == []  # not run here: it is waiting for its own thread
    assert json.loads(manager._commands.get_nowait())["payload"]["action"] == "press"


@pytest.mark.parametrize(
    "change",
    [
        {"programId": ""},
        {"widgetId": None},
        {"programId": 3},
        {"action": 5},
        {"values": [1, 2]},
    ],
    ids=["empty program", "no widget", "program not text", "action not text", "values not an object"],
)
def test_a_malformed_command_is_refused_with_400(unit, change):
    manager, _, grant_, _ = unit
    result = send_command(manager, grant_, {**GOOD, **change})
    assert result["status"] == 400 and result["id"] == 7
    assert manager._commands.empty()


def test_an_oversized_command_is_refused(unit):
    manager, _, grant_, _ = unit
    assert send_command(manager, grant_, GOOD, size=pm.MAX_COMMAND_BYTES + 1)["status"] == 400


def test_a_command_from_an_expired_session_is_refused_with_409(unit):
    manager, _, grant_, clock = unit
    clock.now += 10_000
    assert send_command(manager, grant_, GOOD)["status"] == 409
    assert manager._commands.empty()


def test_a_viewer_cannot_send_faster_than_the_limit_and_the_allowance_comes_back(unit):
    manager, _, grant_, clock = unit
    statuses = [send_command(manager, grant_, {**GOOD, "id": n})["status"] for n in range(pm.COMMAND_BURST + 5)]
    assert statuses.count(202) == pm.COMMAND_BURST and statuses[-1] == 429
    while not manager._commands.empty():  # what the device's worker would have done meanwhile
        manager._commands.get_nowait()
    clock.now += 1.0  # a second later: about 30 more may pass
    later = [send_command(manager, grant_, {**GOOD, "id": n})["status"] for n in range(40)]
    assert 25 <= later.count(202) <= 31 and later[-1] == 429


def test_one_viewers_limit_does_not_use_up_anothers(wire, clock):
    manager = PeerManager(wire, DeviceLimits(max_viewer_sessions=2), clock=clock, device_id="birdbox01", on_command=Handler())
    first = open_grant(manager, clock)
    manager._grants["s2"] = pm._Grant(session_id="s2", ttl_seconds=900, deadline=clock() + 900, expires_at="x", channel=FakeChannel())
    second = manager._grants["s2"]
    for n in range(pm.COMMAND_BURST + 1):
        send_command(manager, first, {**GOOD, "id": n})
    assert send_command(manager, second, GOOD)["status"] == 202


def test_a_busy_device_refuses_rather_than_queueing_without_end(unit):
    manager, _, grant_, _ = unit
    for _ in range(pm.COMMAND_QUEUE):
        manager._commands.put_nowait("{}")
    assert send_command(manager, grant_, GOOD)["status"] == 429


def test_a_device_with_no_command_handler_says_so(wire, clock):
    manager = PeerManager(wire, DeviceLimits(), clock=clock, device_id="birdbox01")
    assert send_command(manager, open_grant(manager, clock), GOOD)["status"] == 503


def test_an_id_that_is_not_text_or_a_number_is_not_echoed(unit):
    manager, _, grant_, _ = unit
    assert send_command(manager, grant_, {**GOOD, "id": {"a": 1}})["id"] is None
    assert send_command(manager, grant_, {**GOOD, "id": True})["id"] is None


def test_the_command_worker_runs_the_handler_on_its_own_thread_and_survives_a_handler_that_raises(wire, clock):
    seen = []

    def handler(text):
        if not seen:
            seen.append("raised")
            raise RuntimeError("boom")
        seen.append(json.loads(text)["payload"]["action"])

    manager = PeerManager(wire, DeviceLimits(), clock=clock, device_id="birdbox01", on_command=handler)
    manager.start()
    try:
        grant_ = open_grant(manager, clock)
        send_command(manager, grant_, GOOD)
        send_command(manager, grant_, {**GOOD, "action": "again"})
        end = time.monotonic() + 3
        while len(seen) < 2 and time.monotonic() < end:
            time.sleep(0.01)
        assert seen == ["raised", "again"]
    finally:
        manager.stop()


# --- the Device wiring ------------------------------------------------------------------------------------------------------------


class Recorder:
    def __init__(self):
        self.sent = []

    def send(self, data):
        self.sent.append(data)


def test_a_device_gives_each_runtime_value_to_its_direct_viewers_as_well_as_the_platform():
    from pihub import Device, Program

    transport = Recorder()
    device = Device(id="birdbox01", transport=transport, webrtc=False, device_information=False)
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output("temperature", "Gauge")
    device.add_program(program)
    device.start()

    class Viewers:
        published = []

        def publish_runtime(self, message):
            self.published.append(message)

        def uplink_required(self):
            return True  # a viewer is still connecting: the platform is being sent the value as well

        def stop(self):
            pass

    device._peers = Viewers()
    temperature.set(21.5)

    assert [m.value for m in Viewers.published] == [21.5]
    assert any('"value": 21.5' in raw for raw in transport.sent)  # and the platform still has it
    assert [m.value for m in device._runtime_snapshot()] == [21.5]
    device.stop()


def test_a_direct_viewer_is_told_the_value_even_while_the_platform_connection_is_down():
    from pihub import Device, Program

    class Down:
        def send(self, data):
            raise ConnectionError("platform unreachable")

    device = Device(id="birdbox01", transport=Down(), webrtc=False, device_information=False)
    program = Program(id="birdbox", name="Bird Box")
    temperature = program.output("temperature", "Gauge")
    device.add_program(program)
    device._started = True  # attached without a live connection

    class Viewers:
        published = []

        def publish_runtime(self, message):
            self.published.append(message)

        def uplink_required(self):
            return True

    device._peers = Viewers()
    with pytest.raises(ConnectionError):
        temperature.set(3.0)  # the platform send fails ...
    assert [m.value for m in Viewers.published] == [3.0]  # ... but the direct viewers had it first


def test_the_peer_manager_a_device_builds_carries_its_id_snapshot_and_command_route_and_a_viewer_command_reaches_the_program():
    from pihub import Device, Program

    device = Device(id="birdbox01", transport=Recorder(), webrtc=True, device_information=False)
    program = Program(id="birdbox", name="Bird Box")
    snapshot = program.command("snapshot", "Button")
    pressed = []
    snapshot.handler(lambda command: pressed.append(command.action))
    device.add_program(program)
    device.start()
    try:
        peers = device._peers
        assert peers is not None and peers._device_id == "birdbox01"
        assert peers._snapshot == device._runtime_snapshot and peers._on_command == device.handle_incoming
        peers._on_command(CommandMessage(binding=Binding(device_id="birdbox01", program_id="birdbox", widget_id="snapshot"), action="press").to_json())
        end = time.monotonic() + 3
        while not pressed and time.monotonic() < end:
            time.sleep(0.01)
        assert pressed == ["press"]
    finally:
        device.stop()
