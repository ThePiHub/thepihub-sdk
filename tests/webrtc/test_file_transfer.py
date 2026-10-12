"""Downloading a file over its own pre-negotiated DataChannel (`WebRTC_Architecture.md` > 10.1): the request and
the "ready" reply travel over the main "pihub" channel (no SDP renegotiation - both sides create the transfer
channel themselves, with a matching numeric id); this is what `Files.resolve()` (`tests/test_files_widget.py`)
feeds into, through `resolve_file`.
"""

import asyncio
import json

import pytest

aiortc = pytest.importorskip("aiortc")

from pihub.webrtc import DeviceLimits, PeerManager  # noqa: E402
from pihub.webrtc import peer_manager as pm  # noqa: E402
from tests.webrtc.test_peer_manager import Wire, connect_browser, grant, run, until  # noqa: E402


@pytest.fixture
def wire():
    return Wire()


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


def make(wire, resolve_file=None, **kwargs):
    manager = PeerManager(
        wire, DeviceLimits(max_viewer_sessions=2, **kwargs), heartbeat_seconds=0.2, device_id="birdbox01", resolve_file=resolve_file
    )
    manager.start()
    return manager


def request(channel, transfer_id=pm.FILE_CHANNEL_ID_BASE + 1, **fields):
    channel.send(
        json.dumps({"kind": "file-request", "requestId": 1, "transferId": transfer_id, "programId": "birdbox", "widgetId": "files", **fields})
    )


# --- the happy path: the bytes actually arrive -----------------------------------------------------------------


def test_a_requested_file_arrives_intact_over_its_own_channel(wire, tmp_path):
    content = bytes(range(256)) * 200  # 51,200 bytes: several 16 KiB chunks
    path = tmp_path / "a.mp4"
    path.write_bytes(content)
    manager = make(wire, resolve_file=lambda program, widget, folder, name: path)
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")

            # The browser picks the id and creates its channel FIRST, so it is already listening before the
            # device can possibly finish sending on its own end (see `_on_file_request`'s docstring) - and it
            # must wait for that channel's OWN "open" event, not just create the object, before asking: a
            # negotiated channel's readiness is local to each side and briefly lags its peer's.
            transfer_id = pm.FILE_CHANNEL_ID_BASE + 1
            received = bytearray()
            done = asyncio.Event()
            file_channel = pc.createDataChannel(f"file:{transfer_id}", negotiated=True, id=transfer_id)

            @file_channel.on("message")
            def on_message(data):
                received.extend(data)

            @file_channel.on("close")
            def on_close():
                done.set()

            await until(lambda: file_channel.readyState == "open", what="the browser's own file channel to open")
            request(channel, transfer_id=transfer_id, folder="Videos", name="a.mp4")
            await until(lambda: frames(inbox, "file-ready"), what="file-ready")
            ready = frames(inbox, "file-ready")[0]
            # Completion is `file-complete`, over the main channel - not the per-transfer channel closing, which a
            # real Chrome does not always see (found against a real peer, not a guess): see `_send_file`'s
            # docstring. The channel closing too is still checked here since aiortc-to-aiortc does do that.
            await until(lambda: frames(inbox, "file-complete"), what="file-complete")
            complete = frames(inbox, "file-complete")[0]
            await asyncio.wait_for(done.wait(), timeout=10)
            return ready, complete, bytes(received)

        ready, complete, received = run(scenario())
    finally:
        manager.stop()
    assert ready["size"] == len(content)
    assert complete["requestId"] == ready["requestId"] == 1
    assert complete["transferId"] == pm.FILE_CHANNEL_ID_BASE + 1
    assert received == content


def test_the_slot_is_free_again_once_a_transfer_finishes(wire, tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 10)
    manager = make(wire, resolve_file=lambda program, widget, folder, name: path, max_file_transfers=1)
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            request(channel, folder="Videos", name="a.bin")
            await until(lambda: frames(inbox, "file-ready"), what="the first file-ready")
            await until(lambda: manager._active_file_transfers() == 0, what="the slot to free")
            # a second request now succeeds, where it would have been refused while the first was still open
            request(channel, folder="Videos", name="a.bin")
            await until(lambda: len(frames(inbox, "file-ready")) == 2, what="the second file-ready")

        run(scenario())
    finally:
        manager.stop()


# --- refusals: no round trip needed, or a slot held directly (white-box) to avoid racing a real transfer --------


def test_file_capacity_is_enforced_by_the_device(wire, tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 10)
    manager = make(wire, resolve_file=lambda program, widget, folder, name: path, max_file_transfers=1)
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            manager._grants["s1"].file_transfers[999] = pm._FileTransfer(channel=None, path=path, size=10)
            request(channel, folder="Videos", name="a.bin")
            await until(lambda: frames(inbox, "file-error"), what="the refusal")
            return frames(inbox, "file-error")[0]

        error = run(scenario())
    finally:
        manager.stop()
    assert error["reason"] == "file_capacity_reached"


def test_a_file_resolve_file_refuses_is_not_found(wire, tmp_path):
    manager = make(wire, resolve_file=lambda program, widget, folder, name: None)
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            request(channel, folder="Videos", name="missing.mp4")
            await until(lambda: frames(inbox, "file-error"), what="the refusal")
            return frames(inbox, "file-error")[0]

        error = run(scenario())
    finally:
        manager.stop()
    assert error["reason"] == "not_found"


def test_a_request_missing_fields_is_a_bad_request(wire, tmp_path):
    manager = make(wire, resolve_file=lambda program, widget, folder, name: tmp_path / "a.bin")
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            channel.send(json.dumps({"kind": "file-request", "requestId": 1, "programId": "birdbox", "widgetId": "files"}))
            await until(lambda: frames(inbox, "file-error"), what="the refusal")
            return frames(inbox, "file-error")[0]

        error = run(scenario())
    finally:
        manager.stop()
    assert error["reason"] == "bad_request"


def test_no_resolver_configured_refuses_every_request(wire, tmp_path):
    manager = make(wire)  # no resolve_file at all: an older program with no Files widget
    try:

        async def scenario():
            manager.handle_signal(grant("s1"))
            pc, channel, inbox = await connect_browser(manager, wire, "s1")
            await until(lambda: frames(inbox, "heartbeat"), what="the channel to open")
            request(channel, folder="Videos", name="a.bin")
            await until(lambda: frames(inbox, "file-error"), what="the refusal")
            return frames(inbox, "file-error")[0]

        error = run(scenario())
    finally:
        manager.stop()
    assert error["reason"] == "unavailable"
