"""Live video on the device (Phase 5), against a real WebRTC peer that receives and decodes the frames.

The source is the synthetic test pattern (no camera needed), the encoder is aiortc's real H.264, and the
"browser" is a real aiortc peer negotiating video IN-BAND over the DataChannel, exactly as the frontend does.
"""

import asyncio
import json
import re
import time

import pytest

aiortc = pytest.importorskip("aiortc")

from aiortc import RTCPeerConnection  # noqa: E402

from pihub.protocol.messages import (  # noqa: E402
    SIGNAL_ACCEPT,
    SIGNAL_ANSWER,
    SIGNAL_CLOSE,
    SIGNAL_MEDIA,
    SIGNAL_OFFER,
    SIGNAL_REJECT,
    SIGNAL_USAGE,
    SignalMessage,
)
from pihub.video import CameraUnavailable, TestPatternSource, VideoSource  # noqa: E402
from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402
from tests.webrtc.test_peer_manager import FakeClock, Wire, connect_browser, grant, run, until  # noqa: E402

STREAM = "birdbox.camera"


class FailingSource(VideoSource):
    def start(self):
        raise CameraUnavailable("no camera attached")


class Recorder(TestPatternSource):
    """A test pattern that remembers how it was started and stopped."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.starts = self.stops = 0  # calls to start()/stop(): users of the camera
        self.opens = self.closes = 0  # the camera actually opening/closing (it is shared, so these differ)

    def start(self):
        self.starts += 1
        super().start()

    def stop(self):
        self.stops += 1
        super().stop()

    def _open(self):
        self.opens += 1
        super()._open()

    def _close(self):
        self.closes += 1
        super()._close()


@pytest.fixture
def wire():
    return Wire()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def source():
    return Recorder(size=(640, 480), fps=15)


@pytest.fixture
def manager(wire, clock, source):
    peers = PeerManager(wire, DeviceLimits(max_viewer_sessions=2, max_media_sessions=1), clock=clock, heartbeat_seconds=0.2)
    peers.set_video_sources({STREAM: source})
    peers.start()
    yield peers
    peers.stop()


def dc_messages(inbox):
    out = []
    for raw in list(inbox):
        try:
            out.append(json.loads(raw))
        except ValueError:
            pass
    return out


def signal_on_channel(inbox, kind):
    return [m["payload"] for m in dc_messages(inbox) if m.get("messageType") == "signal" and m["payload"].get("kind") == kind]


async def start_session(manager, wire, session_id="s1"):
    manager.handle_signal(grant(session_id, ttl=10_000_000))  # long: these tests move a fake clock a long way
    await wire.wait_for("accept", session_id)
    pc, channel, inbox = await connect_browser(manager, wire, session_id)
    await until(lambda: channel.readyState == "open", what="the data channel")
    return pc, channel, inbox


async def grant_media(manager, wire, session_id="s1", count=1):
    manager.handle_signal(SignalMessage(SIGNAL_MEDIA, session_id, {"action": "grant"}))
    await until(lambda: len(wire.find("accept", session_id)) >= count + 1 or wire.find("reject", session_id), what="the media reply")


async def offer_video(pc, channel, inbox, session_id="s1"):
    """What the frontend does: a recvonly video transceiver (REUSED on a second start), offered IN-BAND,
    with the device's answer applied. Frames from every start land in the same list."""
    from aiortc import RTCSessionDescription

    if not hasattr(pc, "_video_frames"):
        pc._video_frames, pc._got_track = [], asyncio.Event()

        @pc.on("track")
        def on_track(track):
            pc._got_track.set()

            async def pump():
                try:
                    while True:
                        pc._video_frames.append(await track.recv())
                except Exception:
                    pass

            asyncio.ensure_future(pump())

    existing = [t for t in pc.getTransceivers() if t.kind == "video"]
    if existing:
        existing[0].direction = "recvonly"
    else:
        pc.addTransceiver("video", direction="recvonly")
    await pc.setLocalDescription(await pc.createOffer())
    answers_before = len(signal_on_channel(inbox, "answer"))
    rejects_before = len(signal_on_channel(inbox, "reject"))
    channel.send(SignalMessage(SIGNAL_OFFER, session_id, {"sdp": pc.localDescription.sdp}).to_json())
    await until(
        lambda: len(signal_on_channel(inbox, "answer")) > answers_before or len(signal_on_channel(inbox, "reject")) > rejects_before,
        what="the in-band reply",
    )
    if len(signal_on_channel(inbox, "answer")) <= answers_before:
        return None, pc._video_frames, None
    answer = signal_on_channel(inbox, "answer")[-1]
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type="answer"))
    return answer, pc._video_frames, pc._got_track


# --- the announcement ----------------------------------------------------------------------------------------


def test_a_device_with_a_camera_announces_video_and_one_media_slot(manager, wire):
    async def scenario():
        manager.announce()
        await wire.wait_for("capabilities")

    run(scenario())
    caps = wire.find("capabilities")[0].data
    assert caps["webrtc"]["media"] == ["video"] and caps["limits"]["maxMediaSessions"] == 1


def test_a_device_without_a_camera_announces_no_media_and_no_media_slot(wire, clock):
    peers = PeerManager(wire, DeviceLimits(), clock=clock)
    peers.start()
    try:
        async def scenario():
            peers.announce()
            await wire.wait_for("capabilities")

        run(scenario())
    finally:
        peers.stop()
    caps = wire.find("capabilities")[0].data
    assert caps["webrtc"]["media"] == [] and caps["limits"]["maxMediaSessions"] == 0


# --- the grant -------------------------------------------------------------------------------------------------


def test_a_media_grant_is_accepted_but_starts_no_camera(manager, wire, source):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        await grant_media(manager, wire)

    run(scenario())
    assert len(wire.find("accept", "s1")) == 2
    assert source.starts == 0  # no offer yet, so no camera


@pytest.mark.parametrize("sources,reason", [({}, "no_camera")])
def test_a_grant_for_a_device_with_no_camera_is_rejected(wire, clock, sources, reason):
    peers = PeerManager(wire, DeviceLimits(), clock=clock)
    peers.start()
    try:
        async def scenario():
            peers.handle_signal(grant("s1"))
            await wire.wait_for("accept", "s1")
            peers.handle_signal(SignalMessage(SIGNAL_MEDIA, "s1", {"action": "grant"}))
            await wire.wait_for("reject", "s1")

        run(scenario())
    finally:
        peers.stop()
    assert wire.find("reject", "s1")[0].data["reason"] == reason


def test_the_device_is_authoritative_on_its_own_media_capacity(manager, wire):
    async def scenario():
        manager.handle_signal(grant("s1"))
        manager.handle_signal(grant("s2"))
        await wire.wait_for("accept", "s2")
        await grant_media(manager, wire, "s1")
        manager.handle_signal(SignalMessage(SIGNAL_MEDIA, "s2", {"action": "grant"}))  # the platform said yes
        await wire.wait_for("reject", "s2")

    run(scenario())
    assert wire.find("reject", "s2")[0].data["reason"] == "media_capacity"


def test_a_media_offer_without_a_grant_is_refused_and_no_camera_starts(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            answer, frames, _ = await offer_video(pc, channel, inbox)
            return answer, inbox
        finally:
            await pc.close()

    answer, inbox = run(scenario())
    assert answer is None
    assert signal_on_channel(inbox, "reject")[0]["reason"] == "no_media_grant"
    assert source.starts == 0


# --- real frames -----------------------------------------------------------------------------------------------


def test_video_frames_arrive_decoded_over_the_real_connection_in_h264(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            answer, frames, got_track = await offer_video(pc, channel, inbox)
            assert answer is not None, signal_on_channel(inbox, "reject")
            await asyncio.wait_for(got_track.wait(), 15)
            await until(lambda: len(frames) >= 20, timeout=30, what="20 decoded video frames")
            await asyncio.sleep(1.0)
            return answer, frames, source.running
        finally:
            await pc.close()

    answer, frames, running = run(scenario())
    assert running and source.starts == 1
    assert answer["streamId"] == STREAM
    frame = frames[-1]
    assert (frame.width, frame.height) == (640, 480)  # the size the source produced, not a scaled one
    # H.264 was negotiated: the first payload type on the video line is the H264 one.
    video_line = next(l for l in answer["sdp"].splitlines() if l.startswith("m=video"))
    first_payload = video_line.split()[3]
    rtpmap = next(l for l in answer["sdp"].splitlines() if l.startswith(f"a=rtpmap:{first_payload} "))
    assert "H264" in rtpmap
    # The stream id is the runtime value's, so the browser can match them.
    assert re.search(rf"a=msid:{re.escape(STREAM)} ", answer["sdp"])
    # It is live, not a frozen frame: consecutive frames differ.
    assert bytes(frames[2].planes[0])[:640] != bytes(frames[18].planes[0])[:640] or bytes(frames[2].planes[0]) != bytes(frames[18].planes[0])


def test_releasing_stops_the_camera_and_video_can_be_started_again(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            answer, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 5, timeout=30, what="first frames")

            manager.handle_signal(SignalMessage(SIGNAL_MEDIA, "s1", {"action": "release"}))
            await until(lambda: source.stops >= 1, what="the camera to stop")
            await until(lambda: any(m.get("kind") == "media" and m.get("state") == "released" for m in dc_messages(inbox)), what="the browser to be told")
            assert not source.running

            # A second time, on the same peer connection.
            await grant_media(manager, wire, count=2)
            seen = len(frames)
            answer2, frames2, _ = await offer_video(pc, channel, inbox)
            assert answer2 is not None, signal_on_channel(inbox, "reject")
            await until(lambda: len(frames2) >= seen + 5, timeout=30, what="new frames the second time")
            return source.starts
        finally:
            await pc.close()

    assert run(scenario()) == 2


def test_closing_the_session_stops_the_camera(manager, wire, source):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 3, timeout=30, what="frames")
            manager.handle_signal(SignalMessage(SIGNAL_CLOSE, "s1", {"reason": "logout"}))
            await until(lambda: source.stops >= 1, what="the camera to stop")
        finally:
            await pc.close()

    run(scenario())
    assert not source.running and manager.session_ids() == []


def test_a_camera_that_will_not_start_is_refused_and_the_platform_is_told(wire, clock):
    peers = PeerManager(wire, DeviceLimits(), clock=clock, heartbeat_seconds=0.2)
    peers.set_video_sources({STREAM: FailingSource()})
    peers.start()
    try:
        async def scenario():
            pc, channel, inbox = await start_session(peers, wire)
            try:
                await grant_media(peers, wire)
                answer, _, _ = await offer_video(pc, channel, inbox)
                await wire.wait_for("usage", "s1")
                return answer, inbox
            finally:
                await pc.close()

        answer, inbox = run(scenario())
    finally:
        peers.stop()
    assert answer is None and signal_on_channel(inbox, "reject")[0]["reason"] == "media_failed"
    assert wire.find("usage", "s1")[0].data["mediaActive"] is False  # the platform's counter follows


# --- the timers: a camera nobody watches must stop by itself (WebRTC_Architecture.md 9.7) -------------------------


def test_inactivity_warns_at_15_minutes_and_releases_a_minute_later(manager, wire, source, clock):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 3, timeout=30, what="frames")

            clock.now += 899
            await asyncio.sleep(1.3)
            assert not any(m.get("kind") == "still-watching" for m in dc_messages(inbox))
            clock.now += 2  # past 15 minutes without the browser consuming the stream
            await until(lambda: any(m.get("kind") == "still-watching" for m in dc_messages(inbox)), what="the warning")

            clock.now += 61  # ...and no answer
            await wire.wait_for("usage", "s1")
            await until(lambda: source.stops >= 1, what="the camera to stop")
            return dc_messages(inbox)
        finally:
            await pc.close()

    messages = run(scenario())
    assert any(m.get("kind") == "media" and m.get("reason") == "inactive" for m in messages)
    assert wire.find("usage", "s1")[0].data == {"mediaActive": False}


def test_a_browser_that_is_watching_keeps_the_stream_alive(manager, wire, source, clock):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 3, timeout=30, what="frames")
            for _ in range(3):
                clock.now += 600
                channel.send(json.dumps({"kind": "keepalive", "active": True}))
                await asyncio.sleep(0.4)
                clock.now += 0
            await asyncio.sleep(1.3)
            return source.running
        finally:
            await pc.close()

    assert run(scenario()) is True  # 1800 s of clock, but never 900 s without a keepalive


def test_a_hidden_or_paused_tab_does_not_count_as_watching(manager, wire, source, clock):
    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 3, timeout=30, what="frames")
            clock.now += 500
            channel.send(json.dumps({"kind": "keepalive", "active": False}))  # tab hidden: NOT activity
            clock.now += 500
            await until(lambda: any(m.get("kind") == "still-watching" for m in dc_messages(inbox)), what="the warning")
        finally:
            await pc.close()

    run(scenario())


def test_a_grant_the_browser_never_follows_with_an_offer_is_given_back(manager, wire, clock, source):
    async def scenario():
        manager.handle_signal(grant("s1"))
        await wire.wait_for("accept", "s1")
        await grant_media(manager, wire)
        clock.now += 31
        await wire.wait_for("usage", "s1")

    run(scenario())
    assert wire.find("usage", "s1")[0].data == {"mediaActive": False}
    assert source.starts == 0


def test_the_camera_stops_when_the_viewer_vanishes_mid_stream(manager, wire, source):
    """A closed tab or a crashed browser must not leave the camera encoding for nobody (found on the real Pi)."""

    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        await grant_media(manager, wire)
        _, frames, _ = await offer_video(pc, channel, inbox)
        await until(lambda: len(frames) >= 5, timeout=30, what="frames")
        assert source.running
        await pc.close()  # the viewer is gone; nobody sent a release
        await until(lambda: source.stops >= 1, timeout=40, what="the camera to stop")
        await wire.wait_for("usage", "s1", timeout=10)

    run(scenario())
    assert not source.running
    assert wire.find("usage", "s1")[0].data == {"mediaActive": False}  # so the platform's counter frees the slot


def test_a_snapshot_during_live_video_uses_the_stream_and_does_not_stop_it(manager, wire, source, tmp_path):
    """The Snapshot button pressed while someone is watching: a real picture, and the viewer's video carries on."""
    from pihub.video import capture_still

    async def scenario():
        pc, channel, inbox = await start_session(manager, wire)
        try:
            await grant_media(manager, wire)
            _, frames, _ = await offer_video(pc, channel, inbox)
            await until(lambda: len(frames) >= 10, timeout=30, what="frames")

            path = await asyncio.get_running_loop().run_in_executor(None, capture_still, source, tmp_path)

            seen = len(frames)
            await until(lambda: len(frames) >= seen + 10, timeout=30, what="the video to carry on after the snapshot")
            # Checked WHILE the viewer is still connected (closing the viewer rightly closes the camera afterwards).
            return path, source.running, (source.opens, source.closes)
        finally:
            await pc.close()

    path, running, (opens, closes) = run(scenario())
    assert path.exists() and path.stat().st_size > 0 and running
    assert (opens, closes) == (1, 0)  # the snapshot shared the camera: it neither reopened nor closed it
