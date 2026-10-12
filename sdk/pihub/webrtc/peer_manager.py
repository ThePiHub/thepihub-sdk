"""PeerManager: this device's side of viewer sessions and peer connections.

What it does:

```text
open          the platform grants a viewer session; the device counts it, accepts
              (or refuses: it is authoritative on its own capacity) and starts a
              countdown that it enforces ITSELF
offer         the browser's offer: build a peer connection, answer it
candidate     the browser's trickled ICE candidates
renew         restart the countdown
close         tear the session down
(always)      report state and path; heartbeat over the DataChannel; on a
              reconnect, announce capabilities and resync the live sessions
```

**The grant is enforced here, on a monotonic clock.** The platform sends a
duration (`ttlSeconds`), not a timestamp, because a Raspberry Pi has no
real-time clock - until NTP settles its wall clock may be wrong by years. The
device counts the duration from receipt, so a lost revocation can never leave a
peer connection authorized past its grant, even if the platform is unreachable.

**Threading.** The SDK is thread-based and `aiortc` is asyncio. This owns one
private event loop on a daemon thread, and one sender thread that writes to the
control connection; nothing here ever blocks, or raises into, the receive loop
that calls `handle_signal`. A failure in this module is logged and contained: it
must never be able to take down the device's control connection.

**It never opens a port.** A device accepts a grant only from its platform
WebSocket, and its ICE agent only answers checks for sessions it holds a grant
for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from pihub.protocol.bindings import Binding
from pihub.protocol.messages import (
    SIGNAL_ACCEPT,
    SIGNAL_ANSWER,
    SIGNAL_CANDIDATE,
    SIGNAL_CAPABILITIES,
    SIGNAL_CLOSE,
    SIGNAL_MEDIA,
    SIGNAL_OFFER,
    SIGNAL_OPEN,
    SIGNAL_REJECT,
    SIGNAL_RENEW,
    SIGNAL_RESYNC,
    SIGNAL_STATE,
    SIGNAL_USAGE,
    CommandMessage,
    RuntimeMessage,
    SignalMessage,
)
from pihub.webrtc.limits import DeviceLimits

logger = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 5.0
TICK_SECONDS = 1.0
# Live video: a stream nobody is consuming must not run forever.
MEDIA_INACTIVITY_SECONDS = 15 * 60.0  # then warn over the DataChannel ("Still watching?")
MEDIA_WARNING_SECONDS = 60.0  # then release the grant and stop the camera
MEDIA_OFFER_SECONDS = 30.0  # a grant the browser never follows with an offer is released
MAX_INBAND_BYTES = 64 * 1024
CHANNEL_LABEL = "pihub"
# Commands over the DataChannel: the channel exists only because the platform authorized this viewer session, so it carries
# that authorization; the device still limits how fast, and how big, and only while the grant is alive.
MAX_COMMAND_BYTES = 16 * 1024
COMMAND_BURST = 50  # commands one viewer may send at once ...
COMMAND_PER_SECOND = 30.0  # ... and how fast that allowance comes back
COMMAND_QUEUE = 64  # commands waiting for the device's handlers; more than this is refused as busy
# Each side says what it can do when the channel opens, and only relies on the direct path once the OTHER side has said it can: an older device or page (or one still open in a tab) that
# does not know about direct runtime and commands is then left on the platform's path exactly as before.
DIRECT_FEATURES = ["runtime", "command"]

# Files (`WebRTC_Architecture.md` > 10.1): each transfer is its own PRE-NEGOTIATED DataChannel (both sides create
# it with a matching numeric id, `negotiated=True`) so a download needs no SDP renegotiation at all - only the
# request and the ready reply travel over the main "pihub" channel. The BROWSER picks the id and must create its
# channel before it sends the request (see `_on_file_request`); this range is well clear of ids aiortc/the browser
# hand out on their own to a channel created with none, so the device can refuse a request outside it as malformed.
# Proven on a real Pi 3A+ over real wifi (not a guess): 256 KiB gives only one 16 KiB chunk of headroom above the
# threshold, so almost every chunk has to wait for its own `bufferedamountlow` round trip - about 70 of them to
# send 1.1 MB, taking long enough to trip a 15 s frontend timeout on an otherwise-successful transfer. 1 MiB gives
# the SCTP layer enough chunks in flight to pipeline sends instead of serialising them behind each event.
FILE_CHUNK_BYTES = 16 * 1024
FILE_BUFFERED_AMOUNT_LOW_THRESHOLD = 1024 * 1024
FILE_CHANNEL_ID_BASE = 1000
FILE_CHANNEL_ID_RANGE = 4000
MAX_FILE_REQUEST_BYTES = 2 * 1024

PATH_DIRECT = "direct"
PATH_TURN = "turn"


def webrtc_available() -> bool:
    """Whether the optional WebRTC library is installed."""
    try:
        import aiortc  # noqa: F401
    except Exception:  # not installed, or a broken native dependency
        return False
    return True


@dataclass
class _FileTransfer:
    channel: Any
    path: Path
    size: int


@dataclass
class _Grant:
    session_id: str
    ttl_seconds: float
    deadline: float  # monotonic
    expires_at: str  # the platform's own string, echoed back in a resync
    ice_servers: List[Dict[str, Any]] = field(default_factory=list)
    pc: Any = None
    channel: Any = None
    heartbeat: Optional["asyncio.Task"] = None
    watch: Optional["asyncio.Task"] = None
    candidates_received: int = 0  # `candidate` signals applied for the current peer connection
    # Live video
    media: bool = False  # the platform granted this session the camera
    media_active: bool = False  # a track is being sent
    media_starting: bool = False
    media_track: Any = None
    media_source: Any = None
    media_epoch: float = 0.0  # one clock for every track on this peer connection (RTP timestamps must not go back)
    media_requested_at: float = 0.0
    last_active: float = 0.0  # the browser last said it was consuming the stream (monotonic)
    warned_at: Optional[float] = None
    state: str = "ready"
    direct_data: bool = False  # the browser has said it takes runtime values over this channel (a `hello`); until then the platform must keep sending them
    command_tokens: float = float(COMMAND_BURST)  # a bucket, refilled with the clock
    command_at: float = 0.0
    path: Optional[str] = None
    reported: Optional[str] = None  # the last state we told the platform
    file_transfers: Dict[int, _FileTransfer] = field(default_factory=dict)


class PeerManager:
    """One per `Device`. `send` writes a `SignalMessage` to the control connection."""

    def __init__(
        self,
        send: Callable[[SignalMessage], None],
        limits: Optional[DeviceLimits] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
        inactivity_seconds: float = MEDIA_INACTIVITY_SECONDS,
        warning_seconds: float = MEDIA_WARNING_SECONDS,
        media_offer_seconds: float = MEDIA_OFFER_SECONDS,
        device_id: str = "",
        snapshot: Optional[Callable[[], Sequence[RuntimeMessage]]] = None,
        on_command: Optional[Callable[[str], None]] = None,
        on_uplink: Optional[Callable[[bool], None]] = None,
        resolve_file: Optional[Callable[[str, str, str, str], Optional[Path]]] = None,
    ) -> None:
        """`snapshot()` returns the device's latest value of every runtime endpoint, sent to a viewer whose channel has just opened. `on_command(json)`
        receives a `CommandMessage` (as JSON, for `device_id`) that a viewer sent over its channel; it is called on its own thread, never on the WebRTC loop.
        `on_uplink(required)` is called when `uplink_required()` changes (on the WebRTC loop: it must return quickly).
        `resolve_file(programId, widgetId, folder, name)` answers a download request with the real path, or None to
        refuse it (called on the WebRTC loop: it must return quickly and never raise)."""
        self._send = send
        self._device_id = device_id
        self._snapshot = snapshot
        self._on_command = on_command
        self._on_uplink = on_uplink
        self._resolve_file = resolve_file
        self._uplink = False  # nobody is watching yet
        self._commands: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=COMMAND_QUEUE)
        self._command_worker: Optional[threading.Thread] = None
        self._limits = limits or DeviceLimits()
        self._clock = clock
        self._heartbeat_seconds = heartbeat_seconds
        self._inactivity_seconds = inactivity_seconds
        self._warning_seconds = warning_seconds
        self._media_offer_seconds = media_offer_seconds
        self._video_sources: Dict[str, Any] = {}
        self._grants: Dict[str, _Grant] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._outbox: "queue.Queue[Optional[SignalMessage]]" = queue.Queue(maxsize=256)
        self._sender: Optional[threading.Thread] = None
        self._ticker: Optional["asyncio.Task"] = None
        self._ready = threading.Event()
        self._running = False

    # -- lifecycle (any thread) -------------------------------------------------

    @property
    def limits(self) -> DeviceLimits:
        return self._limits

    def start(self) -> None:
        """Start the private event loop and the sender. Safe to call once."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, name="pihub-webrtc", daemon=True)
        self._thread.start()
        self._sender = threading.Thread(target=self._run_sender, name="pihub-webrtc-send", daemon=True)
        self._sender.start()
        if self._on_command is not None:
            self._command_worker = threading.Thread(target=self._run_commands, name="pihub-webrtc-commands", daemon=True)
            self._command_worker.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        """Close every peer connection and stop both threads."""
        if not self._running:
            return
        self._running = False
        loop = self._loop
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                future.result(timeout=5)
            except Exception:
                logger.debug("webrtc shutdown did not finish cleanly", exc_info=True)
            loop.call_soon_threadsafe(loop.stop)
        for wake in (self._outbox, self._commands):
            try:
                wake.put_nowait(None)
            except queue.Full:
                pass
        for thread in (self._thread, self._sender, self._command_worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)

    def set_video_sources(self, sources: Dict[str, Any]) -> None:
        """`{streamId: source}` for the cameras this device has. Announced in `capabilities`.

        With none, the device announces no media and a `maxMediaSessions` of 0, so the platform refuses a
        video request cleanly (`device_has_no_camera`) rather than the device failing it later.
        """
        self._video_sources = dict(sources)

    def announce(self) -> None:
        """Tell the platform what this device can do, and which sessions it holds.

        Called after every (re)connection of the control socket. `capabilities`
        first, then `resync`: a device that reconnects to a freshly restarted
        platform lets it re-admit the peer connections that survived (6.4).
        """
        self._call(self._announce)

    def handle_signal(self, message: SignalMessage) -> None:
        """A `signal` arrived on the control connection (called on the receive thread)."""
        self._call(lambda: self._spawn(self._handle(message)))

    def session_ids(self) -> List[str]:
        """The viewer sessions currently granted. For tests and diagnostics."""
        return list(self._grants)

    def publish_runtime(self, message: RuntimeMessage) -> None:
        """A runtime value was published: send it to every viewer whose DataChannel is open (any thread).

        The platform's WebSocket gets the same value too (a "dual send"): the browser uses whichever it is connected through.
        """
        if not self._running or not self._grants:
            return
        self._call(lambda: self._fan_out_runtime(message))

    def uplink_required(self) -> bool:
        """Whether the platform needs this device's runtime values ("demand-driven uplink").

        Not while nobody is watching, and not while every viewer is connected directly AND has said it takes values that way (an older page does not): then only notifications go
        up. Yes while any viewer is still connecting, has failed, or dropped (the platform relays to them, and the dashboard is never blank while a connection is made), and always when
        this manager is not running (no direct path exists at all).
        """
        if not self._running:
            return True
        return any(not (grant.state == "connected" and grant.direct_data) for grant in list(self._grants.values()))

    def _uplink_check(self) -> None:
        """Tell the device when the answer to `uplink_required()` changes, so it can send the platform a snapshot the moment it starts to matter."""
        required = self.uplink_required()
        if required == self._uplink:
            return
        self._uplink = required
        if self._on_uplink is not None:
            try:
                self._on_uplink(required)
            except Exception:
                logger.exception("webrtc: the uplink callback raised (contained)")

    # -- plumbing -------------------------------------------------------------------

    def _call(self, function: Callable[[], None]) -> None:
        loop = self._loop
        if not self._running or loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._guard(function))
        except RuntimeError:  # the loop closed between the check and the call
            pass

    @staticmethod
    def _guard(function: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                function()
            except Exception:
                logger.exception("webrtc: unexpected error (contained; the control connection is unaffected)")

        return run

    def _spawn(self, coroutine) -> None:
        task = asyncio.ensure_future(coroutine)

        def done(finished: "asyncio.Task") -> None:
            if not finished.cancelled() and finished.exception() is not None:
                logger.error("webrtc: task failed: %r", finished.exception())

        task.add_done_callback(done)

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ticker = loop.create_task(self._tick())
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            finally:
                loop.close()

    def _run_sender(self) -> None:
        while True:
            message = self._outbox.get()
            if message is None:
                return
            try:
                self._send(message)
            except Exception as error:  # a dropped control connection: the platform resyncs later
                logger.debug("webrtc: could not send %s (%s)", message.kind, error)

    def _emit(self, message: SignalMessage) -> None:
        try:
            self._outbox.put_nowait(message)
        except queue.Full:
            logger.warning("webrtc: outbox full, dropping a %s signal", message.kind)

    # -- announcing -------------------------------------------------------------------

    def _announce(self) -> None:
        limits = self._limits.to_dict()
        if not self._video_sources:
            limits["maxMediaSessions"] = 0
        self._emit(
            SignalMessage(
                SIGNAL_CAPABILITIES,
                None,
                {
                    "webrtc": {"dataChannel": True, "media": ["video"] if self._video_sources else []},
                    "limits": limits,
                },
            )
        )
        sessions = [
            {
                "viewerSessionId": grant.session_id,
                "expiresAt": grant.expires_at,
                "state": grant.state,
                "path": grant.path,
            }
            for grant in self._grants.values()
        ]
        self._emit(SignalMessage(SIGNAL_RESYNC, None, {"sessions": sessions}))

    # -- handling signals -----------------------------------------------------------------

    async def _handle(self, message: SignalMessage) -> None:
        kind = message.kind
        if kind == SIGNAL_OPEN:
            self._on_open(message)
        elif kind == SIGNAL_OFFER:
            await self._on_offer(message)
        elif kind == SIGNAL_CANDIDATE:
            await self._on_candidate(message)
        elif kind == SIGNAL_RENEW:
            self._on_renew(message)
        elif kind == SIGNAL_MEDIA:
            await self._on_media(message)
        elif kind == SIGNAL_CLOSE:
            await self._close(message.viewer_session_id, notify=False)
        # Anything else (a kind from the future, or one only a device sends): ignored.

    def _on_open(self, message: SignalMessage) -> None:
        session_id = message.viewer_session_id
        data = message.data
        ttl = data.get("ttlSeconds")
        if not session_id or not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "unsupported"}))
            return
        # This device is authoritative on its own capacity, whatever the platform allowed.
        if session_id not in self._grants and len(self._grants) >= self._limits.max_viewer_sessions:
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "busy"}))
            return
        ice = data.get("iceServers")
        self._grants[session_id] = _Grant(
            session_id=session_id,
            ttl_seconds=float(ttl),
            deadline=self._clock() + float(ttl),
            expires_at=str(data.get("expiresAt", "")),
            ice_servers=[s for s in ice if isinstance(s, dict)] if isinstance(ice, list) else [],
        )
        self._emit(SignalMessage(SIGNAL_ACCEPT, session_id, {}))
        self._uplink_check()  # a viewer is now connecting: the platform needs the values until it is connected directly

    def _on_renew(self, message: SignalMessage) -> None:
        grant = self._grants.get(message.viewer_session_id or "")
        ttl = message.data.get("ttlSeconds")
        if grant is None or not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or ttl <= 0:
            return
        # Restarted from the moment the renew ARRIVES, so signalling latency can only
        # make this side's window shorter than the platform's, never longer.
        grant.ttl_seconds = float(ttl)
        grant.deadline = self._clock() + float(ttl)
        grant.expires_at = str(message.data.get("expiresAt", grant.expires_at))

    def _expired(self, grant: _Grant) -> bool:
        return self._clock() >= grant.deadline

    async def _on_offer(self, message: SignalMessage) -> None:
        session_id = message.viewer_session_id or ""
        grant = self._grants.get(session_id)
        sdp = message.data.get("sdp")
        if grant is None or self._expired(grant) or not isinstance(sdp, str) or not sdp:
            # No grant, or it ran out: refuse, so the platform can fail the request at once.
            self._emit(SignalMessage(SIGNAL_REJECT, session_id or None, {"reason": "no_grant"}))
            if grant is not None:
                await self._close(session_id, notify=True, reason="expired")
            return

        # A new offer replaces any earlier peer connection for this session (a retry).
        await self._teardown_peer(grant)

        from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

        servers = [
            RTCIceServer(urls=s["urls"], username=s.get("username"), credential=s.get("credential"))
            for s in grant.ice_servers
            if s.get("urls")
        ]
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=servers))
        grant.pc = pc
        grant.state = "negotiating"
        grant.reported = None
        grant.candidates_received = 0

        @pc.on("datachannel")
        def on_datachannel(channel) -> None:
            self._attach_channel(grant, channel)

        @pc.on("connectionstatechange")
        async def on_state() -> None:
            await self._on_connection_state(grant, pc)

        try:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)  # gathers ALL local candidates (non-trickle)
        except Exception as error:
            logger.warning("webrtc: could not answer the offer for a session (%s)", type(error).__name__)
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "unsupported"}))
            await self._teardown_peer(grant)
            return
        self._emit(SignalMessage(SIGNAL_ANSWER, session_id, {"sdp": pc.localDescription.sdp}))
        grant.watch = asyncio.ensure_future(self._watch_negotiation(grant, pc))

    async def _on_candidate(self, message: SignalMessage) -> None:
        grant = self._grants.get(message.viewer_session_id or "")
        if grant is None or grant.pc is None or self._expired(grant):
            return
        from aiortc.sdp import candidate_from_sdp

        candidates = message.data.get("candidates")
        for item in candidates if isinstance(candidates, list) else []:
            text = item.get("candidate") if isinstance(item, dict) else None
            if not isinstance(text, str) or not text:
                continue  # an empty candidate is the browser's end-of-candidates marker
            try:
                candidate = candidate_from_sdp(text.split(":", 1)[1] if text.startswith("candidate:") else text)
                candidate.sdpMid = item.get("sdpMid")
                candidate.sdpMLineIndex = item.get("sdpMLineIndex")
                if candidate.sdpMid is None and candidate.sdpMLineIndex is None:
                    candidate.sdpMLineIndex = 0
                await grant.pc.addIceCandidate(candidate)
                grant.candidates_received += 1
            except Exception as error:
                logger.debug("webrtc: ignoring a candidate that would not parse or add (%s)", type(error).__name__)

    # -- peer connection events ---------------------------------------------------------------

    def _attach_channel(self, grant: _Grant, channel: Any) -> None:
        grant.channel = channel
        grant.direct_data = False  # a new channel: wait for this browser's `hello` again

        @channel.on("message")
        def on_message(data: Any) -> None:
            self._on_channel_message(grant, data)

        @channel.on("close")
        def on_close() -> None:
            # No DataChannel means no keepalives and no way to tell the viewer anything: stop the camera.
            if grant.channel is channel and (grant.media or grant.media_active):
                self._spawn(self._stop_media(grant, notify_platform=True, reason="connection_ended"))

        def begin() -> None:
            if grant.heartbeat is None or grant.heartbeat.done():
                grant.heartbeat = asyncio.ensure_future(self._heartbeat(grant, channel))
            self._channel_send(grant, {"kind": "hello", "features": DIRECT_FEATURES})  # first: what this device can do, so the page knows to trust what follows
            self._send_snapshot(grant)  # a viewer that has just connected is told where everything stands, not left to wait for the next change

        if channel.readyState == "open":
            begin()
        else:
            channel.on("open", begin)

    def _on_channel_message(self, grant: _Grant, data: Any) -> None:
        """What the browser says over the DataChannel: a `ping`, whether it is still watching, and in-band
        signalling (an `offer` to start video). Anything else is ignored."""
        if not isinstance(data, str) or len(data) > MAX_INBAND_BYTES:
            return
        try:
            body = json.loads(data)
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        kind = body.get("kind")
        if kind == "ping" and len(data) <= 1024:
            self._channel_send(grant, {"kind": "pong", "t": body.get("t")})
        elif kind == "keepalive" and len(data) <= 1024:
            # Whether the stream is being CONSUMED (tab visible, video playing), not whether a hand moved.
            if body.get("active") is True:
                grant.last_active = self._clock()
                grant.warned_at = None
        elif kind == "hello" and len(data) <= 1024:
            features = body.get("features")
            direct = isinstance(features, list) and "runtime" in features
            if direct != grant.direct_data:
                grant.direct_data = direct
                self._uplink_check()  # this viewer now takes its values over the channel (or does not): the platform's part changes
        elif kind == "command":
            self._on_command_frame(grant, body, len(data))
        elif kind == "file-request" and len(data) <= MAX_FILE_REQUEST_BYTES:
            self._spawn(self._on_file_request(grant, body))
        elif kind == "file-cancel" and len(data) <= MAX_FILE_REQUEST_BYTES:
            self._on_file_cancel(grant, body)
        elif body.get("messageType") == "signal":
            try:
                message = SignalMessage.from_payload(body.get("payload") or {})
            except ValueError:
                return
            if message.kind == SIGNAL_OFFER:
                self._spawn(self._on_inband_offer(grant, message))

    # -- runtime and commands over the DataChannel (Phases 2 and 3) ----------------------------------------------

    @staticmethod
    def _runtime_frame(message: RuntimeMessage) -> str:
        """The frame a browser applies to its runtime store: the same fields as a push from the platform, so a widget cannot tell which path a value took."""
        return json.dumps({"kind": "runtime", **message.to_payload(), "timestamp": time.time()})

    def _fan_out_runtime(self, message: RuntimeMessage) -> None:
        text = self._runtime_frame(message)
        for grant in list(self._grants.values()):
            self._channel_send_text(grant, text)

    def _send_snapshot(self, grant: _Grant) -> None:
        if self._snapshot is None:
            return
        try:
            messages = list(self._snapshot())
        except Exception:
            logger.debug("webrtc: could not take a runtime snapshot", exc_info=True)
            return
        for message in messages:
            self._channel_send_text(grant, self._runtime_frame(message))

    def _on_command_frame(self, grant: _Grant, body: Dict[str, Any], size: int) -> None:
        """A viewer sent a command. Answer at once (`command-result`: 202 handed to the device, or why not); the handlers run on their own thread.

        Success means "handed to the device", not "done", exactly as for the HTTP relay.
        """
        command_id = body.get("id")
        if isinstance(command_id, bool) or not isinstance(command_id, (str, int)):
            command_id = None

        def answer(status: int, detail: Optional[str] = None) -> None:
            result: Dict[str, Any] = {"kind": "command-result", "id": command_id, "status": status}
            if detail:
                result["detail"] = detail
            self._channel_send(grant, result)

        if self._on_command is None or not self._device_id:
            answer(503, "this device does not take commands over the direct connection")
            return
        if size > MAX_COMMAND_BYTES:
            answer(400, "command too large")
            return
        if self._expired(grant):
            answer(409, "session expired")
            return
        now = self._clock()
        if grant.command_at:
            grant.command_tokens = min(float(COMMAND_BURST), grant.command_tokens + (now - grant.command_at) * COMMAND_PER_SECOND)
        grant.command_at = now
        if grant.command_tokens < 1:
            answer(429, "too many commands")
            return
        grant.command_tokens -= 1
        program_id, widget_id = body.get("programId"), body.get("widgetId")
        action, values = body.get("action"), body.get("values")
        if not (isinstance(program_id, str) and program_id and isinstance(widget_id, str) and widget_id):
            answer(400, "a command needs a programId and a widgetId")
            return
        if action is not None and not isinstance(action, str):
            answer(400, "action must be text")
            return
        if values is not None and not isinstance(values, dict):
            answer(400, "values must be an object")
            return
        try:
            message = CommandMessage(binding=Binding(device_id=self._device_id, program_id=program_id, widget_id=widget_id), action=action, values=values)
        except ValueError as error:
            answer(400, str(error))
            return
        try:
            self._commands.put_nowait(message.to_json())
        except queue.Full:
            answer(429, "the device is busy")
            return
        answer(202)

    def _run_commands(self) -> None:
        while True:
            item = self._commands.get()
            if item is None:
                return
            try:
                if self._on_command is not None:
                    self._on_command(item)
            except Exception:
                logger.exception("webrtc: a command handler raised (contained)")

    def _channel_send(self, grant: _Grant, body: Dict[str, Any]) -> None:
        self._channel_send_text(grant, json.dumps(body))

    def _channel_send_text(self, grant: _Grant, text: str) -> None:
        channel = grant.channel
        if channel is not None and channel.readyState == "open":
            try:
                channel.send(text)
            except Exception:
                logger.debug("webrtc: data channel send failed", exc_info=True)

    # -- live video ------------------------------------------------------------------------------------

    async def _on_media(self, message: SignalMessage) -> None:
        """The platform grants (or releases) this session the camera. The device is authoritative: it counts
        its own media sessions and may refuse whatever the platform allowed."""
        session_id = message.viewer_session_id or ""
        grant = self._grants.get(session_id)
        action = message.data.get("action")
        if action == "release":
            if grant is not None:
                await self._stop_media(grant, notify_platform=False, reason="released")
            return
        if action != "grant":
            return
        if grant is None:
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "no_grant"}))
        elif not self._video_sources:
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "no_camera"}))
        elif not grant.media and sum(1 for g in self._grants.values() if g.media) >= self._limits.max_media_sessions:
            self._emit(SignalMessage(SIGNAL_REJECT, session_id, {"reason": "media_capacity"}))
        else:
            now = self._clock()
            grant.media, grant.media_requested_at, grant.last_active, grant.warned_at = True, now, now, None
            self._emit(SignalMessage(SIGNAL_ACCEPT, session_id, {}))

    async def _on_inband_offer(self, grant: _Grant, message: SignalMessage) -> None:
        """The browser's offer to add a video track, sent IN-BAND over the DataChannel: the platform never
        sees it, which is why the grant exists. Refused unless this session holds one."""

        def refuse(reason: str) -> None:
            self._channel_send_text(grant, SignalMessage(SIGNAL_REJECT, grant.session_id, {"reason": reason}).to_json())

        sdp = message.data.get("sdp")
        pc = grant.pc
        if not grant.media:
            return refuse("no_media_grant")
        if pc is None or not isinstance(sdp, str) or not sdp or grant.media_starting:
            return refuse("unavailable")
        stream_id, source = next(iter(self._video_sources.items()))
        encoded = bool(getattr(source, "encoded", False))
        if encoded and not re.search(r"a=rtpmap:\d+ H264/90000", sdp, re.IGNORECASE):
            # Already-encoded H.264 cannot be turned into anything else. Refuse before the camera starts.
            return refuse("codec_unsupported")
        grant.media_starting = True
        try:
            from aiortc import RTCRtpSender, RTCSessionDescription

            from pihub.video.track import SourceTrack

            # The camera starts here, and only here: no grant and offer, no camera.
            await asyncio.get_running_loop().run_in_executor(None, source.start)
            if not grant.media_epoch:
                grant.media_epoch = time.monotonic()
            track = SourceTrack(source, grant.media_epoch)
            sender = pc.addTrack(track)  # creates (or reuses) the video transceiver
            sender._stream_id = stream_id  # the MediaStream id the browser matches to the `streamId` runtime value
            transceiver = next(t for t in pc.getTransceivers() if t.sender is sender)
            # aiortc negotiates codecs INSIDE setRemoteDescription, from the preferences in force at that
            # moment, so H.264 has to be preferred BEFORE the offer is applied, not after.
            codecs = RTCRtpSender.getCapabilities("video").codecs
            h264 = [c for c in codecs if c.mimeType.lower() == "video/h264"]
            # An encoded source can only ever be H.264; a software one prefers it and can fall back.
            transceiver.setCodecPreferences(h264 if encoded else h264 + [c for c in codecs if c not in h264])
            if encoded:
                # A browser asking for a keyframe (it lost its picture) reaches the hardware encoder: aiortc would
                # have forced one from its own encoder, which is not in use here. Ask for one for a new viewer too.
                original_send_keyframe = sender._send_keyframe

                def send_keyframe() -> None:
                    source.request_keyframe()
                    original_send_keyframe()

                sender._send_keyframe = send_keyframe
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await pc.setLocalDescription(await pc.createAnswer())
            if encoded:
                source.request_keyframe()  # the viewer's first picture should not wait for the next regular one
            grant.media_track, grant.media_source, grant.media_active = track, source, True
            grant.last_active, grant.warned_at = self._clock(), None
            self._channel_send_text(
                grant,
                SignalMessage(SIGNAL_ANSWER, grant.session_id, {"sdp": pc.localDescription.sdp, "streamId": stream_id}).to_json(),
            )
        except Exception as error:
            logger.warning("webrtc: could not start video (%s: %s)", type(error).__name__, error)
            await self._stop_media(grant, notify_platform=True, reason="failed")
            refuse("media_failed")
        finally:
            grant.media_starting = False

    async def _stop_media(self, grant: _Grant, notify_platform: bool, reason: str) -> None:
        """End a media grant: stop the track and release the camera. Safe to call any time, any number of times.

        `notify_platform` is true when the platform does not already know (inactivity, the camera failing),
        so its counter follows; the browser is always told over the DataChannel.
        """
        had_media = grant.media or grant.media_active
        if had_media and reason != "released":
            # Visible on purpose: a device that stops its own camera should say why.
            logger.warning("webrtc: video stopped by the device (%s)", reason)
        track, source = grant.media_track, grant.media_source
        grant.media = grant.media_active = False
        grant.media_track = grant.media_source = None
        grant.warned_at = None
        if track is not None:
            # Swap the track out from under the sender first (it idles with none), let a frame that is
            # mid-read finish, and only then stop the track: stopping it under a running sender would end
            # that sender for good, and video could not be started again on this connection.
            if grant.pc is not None:
                for transceiver in grant.pc.getTransceivers():
                    if transceiver.sender.track is track:
                        transceiver.sender.replaceTrack(None)
            await track.wait_idle()
            track.stop()
        if source is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, source.stop)
            except Exception:
                logger.debug("webrtc: stopping the camera raised", exc_info=True)
        if had_media:
            if notify_platform:
                self._emit(SignalMessage(SIGNAL_USAGE, grant.session_id, {"mediaActive": False}))
            self._channel_send(grant, {"kind": "media", "state": "released", "reason": reason})

    # -- files: each transfer is its own pre-negotiated DataChannel, no SDP renegotiation needed -------------------

    def _active_file_transfers(self) -> int:
        return sum(len(g.file_transfers) for g in self._grants.values())

    async def _on_file_request(self, grant: _Grant, body: Dict[str, Any]) -> None:
        """A viewer wants one file. The BROWSER picks `transferId` and creates its matching channel before it even
        sends this request - a pre-negotiated channel is "open" on each side independently of the other, so if the
        device picked the id (and so created, and could finish sending on, its own channel first) a small or fast
        transfer could close before the browser had anything to receive it with. The device is authoritative on
        `maxFileTransfers`, exactly as for media (9.5)."""
        request_id = body.get("requestId")

        def refuse(reason: str) -> None:
            self._channel_send(grant, {"kind": "file-error", "requestId": request_id, "reason": reason})

        program_id, widget_id, folder, name = (body.get(key) for key in ("programId", "widgetId", "folder", "name"))
        transfer_id = body.get("transferId")
        if not all(isinstance(value, str) and value for value in (program_id, widget_id, folder, name)):
            return refuse("bad_request")
        in_range = isinstance(transfer_id, int) and not isinstance(transfer_id, bool) and FILE_CHANNEL_ID_BASE <= transfer_id < FILE_CHANNEL_ID_BASE + FILE_CHANNEL_ID_RANGE
        if not in_range or transfer_id in grant.file_transfers:
            return refuse("bad_request")
        pc = grant.pc
        if pc is None or self._expired(grant):
            return refuse("unavailable")
        if self._resolve_file is None:
            return refuse("unavailable")
        if self._active_file_transfers() >= self._limits.max_file_transfers:
            return refuse("file_capacity_reached")
        try:
            path = self._resolve_file(program_id, widget_id, folder, name)
        except Exception:
            logger.exception("webrtc: resolve_file raised (contained)")
            return refuse("unavailable")
        if path is None:
            return refuse("not_found")
        try:
            size = path.stat().st_size
        except OSError:
            return refuse("not_found")

        try:
            channel = pc.createDataChannel(f"file:{transfer_id}", negotiated=True, id=transfer_id, ordered=True)
        except Exception:
            logger.warning("webrtc: could not open a file channel", exc_info=True)
            return refuse("unavailable")
        channel.bufferedAmountLowThreshold = FILE_BUFFERED_AMOUNT_LOW_THRESHOLD
        transfer = _FileTransfer(channel=channel, path=path, size=size)
        grant.file_transfers[transfer_id] = transfer
        low_water = asyncio.Event()

        @channel.on("bufferedamountlow")
        def _low() -> None:
            low_water.set()

        @channel.on("close")
        def _closed() -> None:
            grant.file_transfers.pop(transfer_id, None)

        def begin() -> None:
            self._channel_send(grant, {"kind": "file-ready", "requestId": request_id, "transferId": transfer_id, "size": size})
            self._spawn(self._send_file(grant, transfer_id, request_id, low_water))

        if channel.readyState == "open":
            begin()
        else:
            channel.on("open", begin)

    def _on_file_cancel(self, grant: _Grant, body: Dict[str, Any]) -> None:
        transfer_id = body.get("transferId")
        transfer = grant.file_transfers.get(transfer_id) if isinstance(transfer_id, int) else None
        if transfer is not None:
            try:
                transfer.channel.close()
            except Exception:
                pass

    async def _send_file(self, grant: _Grant, file_id: int, request_id: Any, low_water: "asyncio.Event") -> None:
        """Stream the file in 16 KiB chunks, backing off while the channel's own buffer is full. Completion is its
        own message (`file-complete`, over the main channel, once every byte is actually flushed) rather than the
        per-transfer channel closing - proven against a real Chrome, not a guess: aiortc closing that channel does
        not reliably reach Chrome as a `close` event, and a browser waiting on it can hang forever even though the
        device sent every byte and no error occurred. The device still closes its own end afterwards, to free the
        channel id, but the browser no longer needs to see that succeed.

        `close()` must wait for `bufferedAmount` to reach 0 first: aiortc (and the spec) let a channel close before
        data still queued for the SCTP transport has actually gone out, silently dropping it - proven with a real
        peer pair, not a guess.
        """
        transfer = grant.file_transfers.get(file_id)
        if transfer is None:
            return
        channel = transfer.channel
        loop = asyncio.get_running_loop()
        ok = False
        try:
            with open(transfer.path, "rb") as handle:
                while True:
                    chunk = await loop.run_in_executor(None, handle.read, FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    if channel.readyState != "open":
                        return
                    if channel.bufferedAmount > FILE_BUFFERED_AMOUNT_LOW_THRESHOLD:
                        low_water.clear()
                        await low_water.wait()
                    channel.send(chunk)
                # Not the "bufferedamountlow" event: it only fires when the buffer CROSSES the threshold downward,
                # which the last, smaller chunk's leftover buffer may already be below without crossing anything.
                while channel.bufferedAmount > 0 and channel.readyState == "open":
                    await asyncio.sleep(0.01)
            ok = True
        except asyncio.CancelledError:
            raise
        except OSError:
            logger.debug("webrtc: file transfer of %s failed", transfer.path, exc_info=True)
        finally:
            grant.file_transfers.pop(file_id, None)
            if ok:
                self._channel_send(grant, {"kind": "file-complete", "requestId": request_id, "transferId": file_id})
            else:
                self._channel_send(grant, {"kind": "file-error", "requestId": request_id, "reason": "transfer_failed"})
            try:
                channel.close()
            except Exception:
                pass

    async def _heartbeat(self, grant: _Grant, channel: Any) -> None:
        sequence = 0
        while channel.readyState == "open" and grant.session_id in self._grants:
            sequence += 1
            self._channel_send(grant, {"kind": "heartbeat", "seq": sequence})
            await asyncio.sleep(self._heartbeat_seconds)

    async def _watch_negotiation(self, grant: _Grant, pc: Any) -> None:
        """While a connection is still being attempted, say what ICE is doing, before anyone gives up.

        The platform abandons a negotiation after 30 seconds, but this side's ICE can take
        longer to conclude, so a failure report can arrive too late to be useful. These
        `checking` reports carry the KINDS of candidate each side has, how many browser
        candidates arrived, and each candidate pair's state (e.g. `host/srflx:FAILED`):
        enough to tell "the browser's candidates never arrived" from "they arrived and every
        pair failed" from "nothing was ever tried". Never an address.
        """
        for delay in (8.0, 10.0, 10.0):
            await asyncio.sleep(delay)
            if grant.pc is not pc or grant.state == "connected" or grant.session_id not in self._grants:
                return
            local, remote = _candidate_kinds(pc)
            self._emit(
                SignalMessage(
                    SIGNAL_STATE,
                    grant.session_id,
                    {
                        "state": "checking",
                        "local": local,
                        "remote": remote,
                        "received": grant.candidates_received,
                        "pairs": _pair_states(pc),
                    },
                )
            )

    async def _on_connection_state(self, grant: _Grant, pc: Any) -> None:
        if grant.pc is not pc:
            return  # an earlier peer connection for a session that has since been retried
        state = pc.connectionState
        if state == "connected":
            path, pair = _selected_path(pc)
            grant.state, grant.path = "connected", path
            self._report(grant, "connected", path=path, pair=pair)
        elif state == "failed":
            # The viewer is gone (a closed tab, a crash, a dead network): the camera must not keep running
            # for nobody. Waiting for the 15-minute inactivity timer would flatten a battery.
            await self._stop_media(grant, notify_platform=True, reason="connection_ended")
            grant.state, grant.path = "failed", None
            local, remote = _candidate_kinds(pc)
            self._report(grant, "failed", local=local, remote=remote, received=grant.candidates_received, pairs=_pair_states(pc))
        elif state in ("disconnected", "closed"):
            if state == "closed":
                await self._stop_media(grant, notify_platform=True, reason="connection_ended")
            if grant.state == "connected":
                grant.state, grant.path = "negotiating", None
                self._report(grant, "disconnected")

    def _report(self, grant: _Grant, state: str, **extra: Any) -> None:
        self._uplink_check()  # the grant's state has just changed
        if grant.reported == state:
            return
        grant.reported = state
        self._emit(SignalMessage(SIGNAL_STATE, grant.session_id, {"state": state, **extra}))

    # -- ending sessions --------------------------------------------------------------------------

    async def _teardown_peer(self, grant: _Grant, notify_media: bool = True) -> None:
        # Whatever ends the peer connection ends its video, and its camera.
        await self._stop_media(grant, notify_platform=notify_media, reason="connection_ended")
        if grant.watch is not None:
            grant.watch.cancel()
            grant.watch = None
        if grant.heartbeat is not None:
            grant.heartbeat.cancel()
            grant.heartbeat = None
        pc, grant.pc, grant.channel = grant.pc, None, None
        grant.file_transfers.clear()  # pc.close() below ends their channels too; this just makes capacity free at once
        if pc is not None:
            try:
                await pc.close()
            except Exception:
                logger.debug("webrtc: closing a peer connection failed", exc_info=True)

    async def _close(self, session_id: Optional[str], notify: bool, reason: str = "device_closed") -> None:
        grant = self._grants.pop(session_id or "", None)
        if grant is None:
            return
        await self._teardown_peer(grant, notify_media=False)  # the platform is ending (or has ended) the session
        self._uplink_check()  # one viewer fewer
        if notify:
            self._emit(SignalMessage(SIGNAL_CLOSE, grant.session_id, {"reason": reason}))

    async def _tick(self) -> None:
        """Enforce every grant's lifetime on the monotonic clock, once a second."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                for grant in [g for g in self._grants.values() if self._expired(g)]:
                    await self._close(grant.session_id, notify=True, reason="expired")
                await self._check_media_timers()
                self._uplink_check()  # a safety net: the answer is also checked at every change
            except Exception:
                logger.exception("webrtc: grant expiry sweep failed")

    async def _check_media_timers(self) -> None:
        """Enforce the video timers, on the device (9.7). A camera nobody watches must stop by itself."""
        now = self._clock()
        for grant in list(self._grants.values()):
            if grant.media_active:
                if grant.warned_at is None and now - grant.last_active >= self._inactivity_seconds:
                    grant.warned_at = now
                    self._channel_send(grant, {"kind": "still-watching", "secondsLeft": int(self._warning_seconds)})
                elif grant.warned_at is not None and now - grant.warned_at >= self._warning_seconds:
                    await self._stop_media(grant, notify_platform=True, reason="inactive")
            elif grant.media and not grant.media_starting and now - grant.media_requested_at >= self._media_offer_seconds:
                # Granted, but the browser never followed with an offer: give the slot back.
                await self._stop_media(grant, notify_platform=True, reason="no_offer")

    async def _shutdown(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        for session_id in list(self._grants):
            await self._close(session_id, notify=False)


CANDIDATE_KINDS = ("host", "srflx", "prflx", "relay")


def _candidate_kinds(pc: Any):
    """Which KINDS of ICE candidate each side has: `(local, remote)`, e.g. `(["host","srflx"], ["host"])`.

    Kinds only (host = a local address, srflx = a public address learned through
    STUN, relay = through a TURN server), never an address. When a direct
    connection fails, this is what tells a NAT that will not let it through (both
    sides have `srflx`, and it still failed) from a network that blocks UDP or STUN
    (`srflx` missing). Reading the remote side uses aioice's internals, so anything
    unexpected gives an empty list rather than raising.
    """

    def kinds(candidates: Any) -> list:
        found = {getattr(c, "type", None) for c in candidates}
        return [kind for kind in CANDIDATE_KINDS if kind in found]

    try:
        ice = pc.sctp.transport.transport
        local = kinds(ice._connection.local_candidates)
    except Exception:
        local = []
    try:
        remote = kinds(ice._connection.remote_candidates)
    except Exception:
        remote = []
    return local, remote


def _pair_states(pc: Any) -> list:
    """The ICE check list as `kind/kind:STATE` strings, e.g. `["host/host:FAILED", "srflx/srflx:IN_PROGRESS*2"]`.

    Kinds and states only. Empty when there are no pairs at all (which itself is the finding:
    nothing was ever tried), or when aioice's internals are not what this expects.
    """
    try:
        counts: dict = {}
        for pair in pc.sctp.transport.transport._connection._check_list:
            key = f"{pair.local_candidate.type}/{pair.remote_candidate.type}:{pair.state.name}"
            counts[key] = counts.get(key, 0) + 1
        return [key if n == 1 else f"{key}*{n}" for key, n in sorted(counts.items())][:12]
    except Exception:
        return []


def _selected_path(pc: Any):
    """`(path, pair)` for the connection ICE settled on.

    `path` is `turn` if either end of the chosen route is a relay candidate, else
    `direct` (host, server-reflexive and peer-reflexive routes all connect the two
    ends without passing through this platform). `pair` is the two candidate
    *types*, e.g. `host/srflx` - a kind of route, never an address. Reading which
    pair was chosen means using aioice's internals, so anything unexpected falls
    back to `direct`, unnamed, rather than raising.
    """
    try:
        ice = pc.sctp.transport.transport  # RTCDtlsTransport -> RTCIceTransport
        nominated = ice._connection._nominated
        pair = next(iter(nominated.values()))
        local, remote = pair.local_candidate.type, pair.remote_candidate.type
    except Exception:
        return PATH_DIRECT, "unknown"
    return (PATH_TURN if "relay" in (local, remote) else PATH_DIRECT), f"{local}/{remote}"
