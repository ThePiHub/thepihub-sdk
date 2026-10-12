"""`PeerManager` against a real WebRTC peer (no server): the device's side of
viewer sessions - grants, capacity, the monotonic expiry, negotiation, the
heartbeat, state reporting and resync.

`aiortc` plays the browser, over real ICE and DTLS between two peers in this
process (host candidates only - no STUN, so no network beyond the machine).
"""

import asyncio
import json
import threading
import time

import pytest

aiortc = pytest.importorskip("aiortc")

from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

from pihub.protocol.messages import (  # noqa: E402
    SIGNAL_ANSWER,
    SIGNAL_CANDIDATE,
    SIGNAL_CLOSE,
    SIGNAL_OFFER,
    SIGNAL_OPEN,
    SIGNAL_RENEW,
    SignalMessage,
)
from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402


class Wire:
    """Collects what the device sends, thread-safely, and lets a test look for a signal."""

    def __init__(self):
        self.messages = []
        self._lock = threading.Lock()

    def __call__(self, message):
        with self._lock:
            self.messages.append(message)

    def kinds(self):
        with self._lock:
            return [m.kind for m in self.messages]

    def find(self, kind, session_id=None):
        with self._lock:
            return [m for m in self.messages if m.kind == kind and (session_id is None or m.viewer_session_id == session_id)]

    async def wait_for(self, kind, session_id=None, count=1, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            found = self.find(kind, session_id)
            if len(found) >= count:
                return found[count - 1]
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {kind!r}; saw {self.kinds()}")


class FakeClock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


def grant(session_id="s1", ttl=900, ice=None):
    return SignalMessage(
        SIGNAL_OPEN,
        session_id,
        {"expiresAt": "2026-09-19T12:00:00Z", "ttlSeconds": ttl, "grants": ["data"], "iceServers": ice or []},
    )


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def manager(wire, clock):
    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=2), clock=clock, heartbeat_seconds=0.2)
    peers.start()
    yield peers
    peers.stop()


async def connect_browser(manager, wire, session_id="s1"):
    """Build a browser-side peer, offer, apply the device's answer. Returns (pc, channel, inbox)."""
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    channel = pc.createDataChannel("pihub")
    inbox = []
    channel.on("message", inbox.append)
    await pc.setLocalDescription(await pc.createOffer())
    manager.handle_signal(SignalMessage(SIGNAL_OFFER, session_id, {"sdp": pc.localDescription.sdp}))
    answer = await wire.wait_for("answer", session_id)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer.data["sdp"], type="answer"))
    return pc, channel, inbox


async def until(predicate, timeout=15.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def run(coroutine):
    return asyncio.run(coroutine)


# --- announcing -------------------------------------------------------------


def test_announce_sends_capabilities_then_resync(manager, wire):
    async def scenario():
        manager.announce()
        await wire.wait_for("resync")

    run(scenario())
    assert wire.kinds() == ["capabilities", "resync"]
    capabilities = wire.find("capabilities")[0]
    assert capabilities.data["webrtc"]["dataChannel"] is True
    # No camera attached, so no media slot is announced (the platform then refuses video cleanly).
    assert capabilities.data["limits"] == {"maxViewerSessions": 2, "maxMediaSessions": 0, "maxFileTransfers": 2}
    assert capabilities.data["webrtc"]["media"] == []
    assert wire.find("resync")[0].data["sessions"] == []


# --- grants ------------------------------------------------------------------------


def test_a_grant_is_accepted(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")

    run(scenario())
    assert manager.session_ids() == ["s1"]


def test_the_device_is_authoritative_on_its_own_capacity(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        manager.handle_signal(grant("s2"))
        await wire.wait_for("accept", "s2")
        manager.handle_signal(grant("s3"))  # the platform said yes; the device says no
        await wire.wait_for("reject", "s3")

    run(scenario())
    assert wire.find("reject", "s3")[0].data["reason"] == "busy"
    assert sorted(manager.session_ids()) == ["s1", "s2"]


@pytest.mark.parametrize("ttl", [0, -5, "900", None, True])
def test_a_grant_without_a_usable_duration_is_refused(manager, wire, ttl):
    async def scenario():
        manager.handle_signal(grant("s1", ttl=ttl))
        await wire.wait_for("reject", "s1")

    run(scenario())
    assert manager.session_ids() == []


def test_an_offer_without_a_grant_is_rejected_at_once(manager, wire):
    async def scenario():
        manager.handle_signal(SignalMessage(SIGNAL_OFFER, "nobody", {"sdp": "v=0\r\n"}))
        await wire.wait_for("reject", "nobody")

    run(scenario())
    assert wire.find("reject", "nobody")[0].data["reason"] == "no_grant"


# --- the grant is enforced here, on a monotonic clock -----------------------------------


def test_a_grant_expires_on_the_devices_own_clock_with_no_word_from_the_platform(manager, wire, clock):
    async def scenario():
        manager.handle_signal(grant("s1", ttl=60))
        await wire.wait_for("accept", "s1")
        clock.now += 59
        await asyncio.sleep(1.3)
        assert manager.session_ids() == ["s1"]
        clock.now += 2  # past 60 seconds of monotonic time
        await wire.wait_for("close", "s1")

    run(scenario())
    assert wire.find("close", "s1")[0].data["reason"] == "expired"
    assert manager.session_ids() == []


def test_renew_restarts_the_countdown_from_arrival(manager, wire, clock):
    async def scenario():
        manager.handle_signal(grant("s1", ttl=60))
        await wire.wait_for("accept", "s1")
        clock.now += 50
        manager.handle_signal(SignalMessage(SIGNAL_RENEW, "s1", {"ttlSeconds": 60, "expiresAt": "2026-09-19T12:10:00Z"}))
        await asyncio.sleep(0.3)
        clock.now += 50  # 100s since the grant, but only 50 since the renew
        await asyncio.sleep(1.3)
        assert manager.session_ids() == ["s1"]
        clock.now += 20
        await wire.wait_for("close", "s1")

    run(scenario())


def test_the_grant_follows_the_monotonic_clock_not_the_wall_clock(wire, monkeypatch):
    """A Pi has no real-time clock: a wall-clock jump must not extend or shorten a grant."""
    peers = PeerManager(wire, DeviceLimits())  # the real, default clock: time.monotonic
    peers.start()
    try:
        real_time = time.time
        monkeypatch.setattr(time, "time", lambda: real_time() + 10 * 365 * 24 * 3600)  # NTP arrives, years later

        async def scenario():
            peers.handle_signal(grant("s1", ttl=60))
            await wire.wait_for("accept", "s1")
            await asyncio.sleep(1.3)
            assert peers.session_ids() == ["s1"]

        run(scenario())
    finally:
        peers.stop()


# --- negotiating -------------------------------------------------------------------------


def test_a_real_peer_connects_and_the_device_reports_state_and_path(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc, channel, inbox = await connect_browser(manager, wire)
        try:
            state = await wire.wait_for("state", "s1")
            assert state.data["state"] == "connected"
            assert state.data["path"] == "direct"
            assert "/" in state.data["pair"]  # two candidate types, never an address

            await until(lambda: channel.readyState == "open", what="the data channel to open")
            await until(lambda: any(json.loads(m)["kind"] == "heartbeat" for m in inbox), what="a heartbeat")
            channel.send(json.dumps({"kind": "ping", "t": 123}))
            await until(lambda: any(json.loads(m) == {"kind": "pong", "t": 123} for m in inbox), what="a pong")
        finally:
            await pc.close()

    run(scenario())


def test_a_dropped_peer_is_reported_disconnected(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc, channel, _ = await connect_browser(manager, wire)
        await wire.wait_for("state", "s1")
        await pc.close()
        await until(lambda: len(wire.find("state", "s1")) >= 2, what="a second state report", timeout=30)

    run(scenario())
    assert wire.find("state", "s1")[-1].data["state"] in ("disconnected", "failed")


def test_a_retry_replaces_the_earlier_peer_connection(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc1, _, _ = await connect_browser(manager, wire)
        await wire.wait_for("state", "s1")
        first = manager._grants["s1"].pc
        # The browser retries with a whole new peer connection and offer.
        wire.messages.clear()
        pc2, channel2, _ = await connect_browser(manager, wire)
        await wire.wait_for("state", "s1")
        assert manager._grants["s1"].pc is not first
        await until(lambda: channel2.readyState == "open")
        await pc1.close()
        await pc2.close()

    run(scenario())


def test_candidates_that_will_not_parse_are_ignored(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc, channel, _ = await connect_browser(manager, wire)
        manager.handle_signal(
            SignalMessage(
                SIGNAL_CANDIDATE,
                "s1",
                {"candidates": [{"candidate": "garbage", "sdpMid": "0", "sdpMLineIndex": 0}, {"candidate": ""}, {"nope": 1}]},
            )
        )
        await asyncio.sleep(0.3)
        assert manager.session_ids() == ["s1"]
        await pc.close()

    run(scenario())


def test_close_from_the_platform_ends_the_session_without_an_echo(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc, channel, _ = await connect_browser(manager, wire)
        await wire.wait_for("state", "s1")
        manager.handle_signal(SignalMessage(SIGNAL_CLOSE, "s1", {"reason": "logout"}))
        await until(lambda: manager.session_ids() == [])
        await asyncio.sleep(0.2)
        assert wire.find("close") == []  # the platform already knows
        await until(lambda: channel.readyState != "open", timeout=30, what="the browser side to notice")
        await pc.close()

    run(scenario())


# --- resync -----------------------------------------------------------------------------------


def test_resync_lists_the_live_sessions_with_the_platforms_own_expiry(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        pc, channel, _ = await connect_browser(manager, wire)
        await wire.wait_for("state", "s1")
        manager.announce()
        await wire.wait_for("resync")
        await pc.close()

    run(scenario())
    sessions = wire.find("resync")[0].data["sessions"]
    assert sessions == [
        {"viewerSessionId": "s1", "expiresAt": "2026-09-19T12:00:00Z", "state": "connected", "path": "direct"}
    ]


# --- containment ---------------------------------------------------------------------------------


def test_a_failing_send_does_not_stop_the_manager():
    calls = []

    def broken(message):
        calls.append(message.kind)
        raise ConnectionError("the control connection is down")

    peers = PeerManager(broken, DeviceLimits())
    peers.start()
    try:
        peers.handle_signal(grant("s1"))
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == ["accept"]
        peers.handle_signal(grant("s2"))  # still working after the failure
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert calls == ["accept", "accept"]
    finally:
        peers.stop()


def test_the_threads_are_daemons_so_they_never_keep_a_process_alive(manager):
    threads = {t.name: t for t in threading.enumerate() if t.name.startswith("pihub-webrtc")}
    assert threads and all(t.daemon for t in threads.values())


def test_stop_ends_both_threads_and_is_safe_twice(wire):
    peers = PeerManager(wire, DeviceLimits())
    peers.start()
    peers.stop()
    peers.stop()
    assert not [t for t in threading.enumerate() if t.name.startswith("pihub-webrtc") and t.is_alive()]


# --- what to report when a direct connection fails (kinds only, never addresses) ---------------


def test_candidate_kinds_are_reported_without_any_address():
    from pihub.webrtc.peer_manager import _candidate_kinds

    class Candidate:
        def __init__(self, kind, host):
            self.type, self.host = kind, host

    class Connection:
        local_candidates = [Candidate("host", "192.168.43.7"), Candidate("srflx", "203.0.113.9"), Candidate("host", "10.0.0.2")]
        remote_candidates = [Candidate("host", "192.168.1.5")]

    class Peer:
        class sctp:
            class transport:
                class transport:
                    _connection = Connection()

    local, remote = _candidate_kinds(Peer())
    assert local == ["host", "srflx"] and remote == ["host"]
    assert not any("." in kind for kind in local + remote)


def test_candidate_kinds_never_raise_on_an_unexpected_peer():
    from pihub.webrtc.peer_manager import _candidate_kinds

    assert _candidate_kinds(object()) == ([], [])


def test_a_failed_state_carries_the_kinds(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")

        class Candidate:
            def __init__(self, kind):
                self.type = kind

        class FailedPeer:
            connectionState = "failed"

            class sctp:
                class transport:
                    class transport:
                        class _connection:
                            local_candidates = [Candidate("host"), Candidate("srflx")]
                            remote_candidates = [Candidate("host")]

        peer = FailedPeer()
        manager._grants["s1"].pc = peer
        await manager._on_connection_state(manager._grants["s1"], peer)
        await wire.wait_for("state", "s1")

    run(scenario())
    failed = wire.find("state", "s1")[-1].data
    assert failed["state"] == "failed" and failed["local"] == ["host", "srflx"] and failed["remote"] == ["host"]


# --- the trickle path: a connection that can ONLY succeed if the device accepts trickled candidates ---------
#
# Over a LAN a browser reaches the device's answer candidates directly and the device never needs the
# browser's trickled ones, so every LAN test passes even if trickle is broken. Across NATs the device has
# to use them to open its own hole. Here the browser gets NONE of the device's candidates, and the device
# gets none in the offer, so only the device's checks toward candidates it received by `candidate` can connect.


def _strip_candidates(sdp: str) -> str:
    return "\r\n".join(
        line for line in sdp.split("\r\n") if not line.startswith("a=candidate") and not line.startswith("a=end-of-candidates")
    )


def test_a_connection_that_needs_the_trickled_candidates_connects(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        channel = pc.createDataChannel("pihub")
        await pc.setLocalDescription(await pc.createOffer())
        offer = pc.localDescription.sdp
        candidates = [line[2:] for line in offer.split("\r\n") if line.startswith("a=candidate:")]
        assert candidates, "the test needs the browser's own candidates to trickle"

        manager.handle_signal(SignalMessage(SIGNAL_OFFER, "s1", {"sdp": _strip_candidates(offer)}))
        answer = await wire.wait_for("answer", "s1")
        await pc.setRemoteDescription(RTCSessionDescription(sdp=_strip_candidates(answer.data["sdp"]), type="answer"))

        # ...and only now, as Chrome does after the answer, the candidates arrive one message at a time.
        manager.handle_signal(
            SignalMessage(
                SIGNAL_CANDIDATE,
                "s1",
                {"candidates": [{"candidate": c, "sdpMid": "0", "sdpMLineIndex": 0} for c in candidates]},
            )
        )
        try:
            await until(lambda: channel.readyState == "open", timeout=25, what="a connection made only from trickled candidates")
        finally:
            await pc.close()

    run(scenario())


# --- progress reports while ICE is still trying -----------------------------------------------------


def test_pair_states_are_summarised_as_kinds_and_states_only():
    from pihub.webrtc.peer_manager import _pair_states

    class State:
        def __init__(self, name):
            self.name = name

    class Candidate:
        def __init__(self, kind, host):
            self.type, self.host = kind, host

    class Pair:
        def __init__(self, local, remote, state):
            self.local_candidate, self.remote_candidate, self.state = Candidate(local, "10.1.2.3"), Candidate(remote, "203.0.113.9"), State(state)

    class Peer:
        class sctp:
            class transport:
                class transport:
                    class _connection:
                        _check_list = [Pair("host", "srflx", "FAILED"), Pair("host", "srflx", "FAILED"), Pair("srflx", "srflx", "IN_PROGRESS")]

    states = _pair_states(Peer())
    assert states == ["host/srflx:FAILED*2", "srflx/srflx:IN_PROGRESS"]
    assert not any(ch.isdigit() and "." in s for s in states for ch in s)  # no address anywhere


def test_pair_states_never_raise_on_an_unexpected_peer():
    from pihub.webrtc.peer_manager import _pair_states

    assert _pair_states(object()) == []


def test_a_device_still_trying_reports_progress_before_the_platform_gives_up(wire, clock):
    """With a peer that never connects, the device sends `checking` reports (kinds, counts, pair states)."""
    import pihub.webrtc.peer_manager as pm

    async def scenario():
        peers = PeerManager(wire, DeviceLimits(), clock=clock)
        peers.start()
        original = asyncio.sleep
        try:
            # Speed the watch up: it waits 8/10/10 real seconds otherwise.
            pm.asyncio.sleep = lambda seconds: original(seconds / 100)
            peers.handle_signal(grant("s1"))
            await wire.wait_for("accept", "s1")
            pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            pc.createDataChannel("pihub")
            await pc.setLocalDescription(await pc.createOffer())
            # The browser never learns the device's candidates and sends none: nothing can connect.
            peers.handle_signal(SignalMessage(SIGNAL_OFFER, "s1", {"sdp": _strip_candidates(pc.localDescription.sdp)}))
            await wire.wait_for("answer", "s1")
            report = await wire.wait_for("state", "s1", timeout=20)
            await pc.close()
            return report
        finally:
            pm.asyncio.sleep = original
            peers.stop()

    report = run(scenario())
    data = report.data
    assert data["state"] == "checking"
    assert data["received"] == 0 and isinstance(data["local"], list) and "host" in data["local"]
    assert isinstance(data["pairs"], list)
