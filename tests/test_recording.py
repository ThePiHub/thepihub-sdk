"""Recording video to the device's storage.

The file (one fragmented MP4, a fragment on the disk per keyframe), the folder (names, the sidecar, the oldest finished
recording deleted first), the recorder (start, stop, the safety cap, its warning, extend) and one encode feeding a stream
and a recorder at once. No camera: packets come from libx264 here, or from `EncodedTestPatternSource`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import pytest

from pihub.video import EncodedTestPatternSource, TestPatternSource
from pihub.video import hardware
from pihub.video.recording import (
    CAMERA_STOPPED,
    MAX_PRE_ROLL_SECONDS,
    MAX_SECONDS,
    STOPPED,
    FragmentedMp4Writer,
    Recorder,
    Recording,
    RecordingStore,
    nal_units,
    parameter_sets,
)

FPS = 15
SIZE = (320, 240)


def encoded_packets(seconds: float, fps: int = FPS):
    """(data, keyframe) pairs as a hardware encoder makes them: Annex B, a keyframe a second with SPS/PPS repeated. Fast, not paced."""
    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height, codec.pix_fmt = SIZE[0], SIZE[1], "yuv420p"
    codec.time_base = Fraction(1, fps)
    codec.framerate = fps
    codec.options = {"tune": "zerolatency", "x264-params": f"repeat-headers=1:keyint={fps}:min-keyint={fps}:scenecut=0"}
    packets = []
    for n in range(int(seconds * fps)):
        frame = av.VideoFrame(SIZE[0], SIZE[1], "yuv420p")
        frame.planes[0].update(bytes((x + n * 7) % 256 for x in range(SIZE[0])) * SIZE[1])
        frame.pts = n
        frame.pict_type = av.video.frame.PictureType.NONE
        packets += [(bytes(p), bool(p.is_keyframe)) for p in codec.encode(frame)]
    packets += [(bytes(p), bool(p.is_keyframe)) for p in codec.encode(None)]
    return packets


def decodable_frames(path: Path) -> int:
    """How many frames a player gets out of the file before it stops (a torn file ends in an error: count up to it)."""
    count = 0
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            try:
                for packet in container.demux(stream):
                    count += len(stream.codec_context.decode(packet))
            except av.error.FFmpegError:
                pass  # the tear: stop reading, but still take the frames the decoder is holding, as a player does
            count += len(stream.codec_context.decode(None))
    except av.error.FFmpegError:
        pass
    return count


def boxes_with_offsets(path: Path):
    """The file's top-level MP4 boxes, as (kind, offset, size)."""
    data, i, boxes = path.read_bytes(), 0, []
    while i + 8 <= len(data):
        size = int.from_bytes(data[i:i + 4], "big") or len(data) - i
        boxes.append((data[i + 4:i + 8].decode("latin1"), i, size))
        i += size
    return boxes


def top_level_boxes(path: Path):
    return [kind for kind, _, _ in boxes_with_offsets(path)]


# --- H.264 helpers --------------------------------------------------------------------------------------------------------------


def test_the_parameter_sets_are_taken_from_a_keyframe():
    key = encoded_packets(0.2)[0][0]
    kinds = [kind for kind, _ in nal_units(key)]
    assert 7 in kinds and 8 in kinds and 5 in kinds
    assert [kind for kind, _ in nal_units(parameter_sets(key))] == [7, 8]
    assert parameter_sets(b"\x00\x00\x00\x01\x41\x9a") == b""  # a delta frame has none


# --- the file --------------------------------------------------------------------------------------------------------------------


def test_packets_become_one_playable_fragmented_mp4_with_a_fragment_per_keyframe(tmp_path):
    path = tmp_path / "rec.mp4"
    writer = FragmentedMp4Writer(path, FPS, SIZE, fsync=False)
    for data, key in encoded_packets(3):
        writer.write(data, key)
    writer.close()
    boxes = top_level_boxes(path)
    assert boxes[:2] == ["ftyp", "moov"] and boxes.count("moof") == 3  # one fragment a second
    assert decodable_frames(path) == 45
    assert writer.duration_seconds == pytest.approx(3.0)
    with av.open(str(path)) as container:
        assert container.streams.video[0].codec_context.name == "h264"  # the encoder's own video, not re-encoded


def test_a_file_starts_at_a_keyframe(tmp_path):
    packets = encoded_packets(2)
    path = tmp_path / "rec.mp4"
    writer = FragmentedMp4Writer(path, FPS, SIZE, fsync=False)
    for data, key in packets[5:]:  # joined mid-GOP: the first ten are delta frames
        writer.write(data, key)
    writer.close()
    assert writer.frames == 15 and decodable_frames(path) == 15


def test_each_fragment_is_on_the_disk_as_soon_as_the_next_keyframe_is_written(tmp_path):
    """The power-cut promise: a copy of the file taken right after a keyframe is written plays every frame before it."""
    path = tmp_path / "rec.mp4"
    writer = FragmentedMp4Writer(path, FPS, SIZE, fsync=True)
    written, checked = 0, 0
    for data, key in encoded_packets(4):
        flushed = writer.write(data, key)
        if flushed:
            snapshot = tmp_path / f"cut-{written}.mp4"
            snapshot.write_bytes(path.read_bytes())  # what a power cut at this moment would leave
            assert decodable_frames(snapshot) == written
            checked += 1
        written += 1
    writer._file.close()  # never finished: no final index written, like a crash
    assert checked == 3


def test_a_file_cut_off_mid_fragment_plays_up_to_the_last_whole_one(tmp_path):
    path = tmp_path / "rec.mp4"
    writer = FragmentedMp4Writer(path, FPS, SIZE, fsync=False)
    for data, key in encoded_packets(3):
        writer.write(data, key)
    writer.close()
    data = path.read_bytes()
    last_mdat_start, last_mdat_size = [(offset, size) for kind, offset, size in boxes_with_offsets(path) if kind == "mdat"][-1]
    cut = tmp_path / "cut.mp4"
    cut.write_bytes(data[: last_mdat_start + last_mdat_size // 2])  # the power fails halfway through writing the third second
    # At least the two whole fragments before the tear (30 frames); a player even gets the torn one's frames that reached the disk.
    assert 30 <= decodable_frames(cut) < 45


def test_camera_timestamps_are_kept_so_a_gap_in_frames_is_a_gap_in_time(tmp_path):
    path = tmp_path / "rec.mp4"
    writer = FragmentedMp4Writer(path, FPS, SIZE, fsync=False)
    for n, (data, key) in enumerate(encoded_packets(2)):
        microseconds = n * 66_667 + (1_000_000 if n >= 15 else 0)  # a second's gap after the first second
        writer.write(data, key, 5_000_000_000 + microseconds)  # the camera's clock does not start at zero
    writer.close()
    assert writer.duration_seconds == pytest.approx(3.0, abs=0.1)


# --- the folder --------------------------------------------------------------------------------------------------------------------


def make_recording(store: RecordingStore, size: int, age: float) -> Path:
    path, _ = store.new_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    path.with_suffix(".json").write_text("{}")
    then = time.time() - age
    os.utime(path, (then, then))
    return path


def test_names_are_never_reused(tmp_path):
    store = RecordingStore(tmp_path)
    first = make_recording(store, 10, 0)
    second, counter = store.new_path()
    assert second != first and counter == 2
    assert second.name.startswith(f"rec-{store.boot_id}-00002-")


def test_the_oldest_finished_recording_is_deleted_first_and_never_the_one_being_written(tmp_path):
    store = RecordingStore(tmp_path, max_total_bytes=2500, min_free_bytes=0, free_bytes=lambda _: 10**12)
    current = make_recording(store, 1000, age=500)  # the OLDEST file, but it is the one being written
    oldest = make_recording(store, 1000, age=300)
    newer = make_recording(store, 1000, age=100)
    unrelated = tmp_path / "notes.txt"
    unrelated.write_text("mine")
    assert store.make_room(current=current)
    assert current.exists() and not oldest.exists() and not oldest.with_suffix(".json").exists()
    assert newer.exists() and unrelated.exists()


def test_when_nothing_is_left_to_delete_and_the_disk_is_still_too_full_it_says_so(tmp_path):
    store = RecordingStore(tmp_path, min_free_bytes=1000, free_bytes=lambda _: 10)
    make_recording(store, 100, age=10)
    assert store.make_room() is False
    assert store.recordings() == []  # it did try: every finished recording went first


# --- the recorder -------------------------------------------------------------------------------------------------------------------


class Notes:
    def __init__(self):
        self.sent = []

    def info(self, message):
        self.sent.append(("info", message))

    def warning(self, message):
        self.sent.append(("warning", message))


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


def recording_file_exists(recorder) -> bool:
    current = recorder.current  # read once: it becomes None the moment a recording ends
    return current is not None and Path(current.path).exists()


def wait_for(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    raise AssertionError("timed out")


@pytest.fixture
def source():
    camera = EncodedTestPatternSource(size=SIZE, fps=FPS)
    yield camera
    while camera.running:
        camera.stop()


def big_disk(_):
    return 10**12


def recorder_for(source, tmp_path, **options):
    notes = options.pop("notify", Notes())
    store = RecordingStore(tmp_path, free_bytes=big_disk)
    return Recorder(source, store=store, notify=notes, fsync=False, **options), notes


def test_a_recording_is_a_complete_playable_file_with_its_sidecar_and_a_notice(source, tmp_path):
    recorder, notes = recorder_for(source, tmp_path)
    saved = []
    recorder.on_saved(saved.append)
    assert recorder.start(reason="test") is True
    assert recorder.recording and source.running  # the camera opened for the recording alone: nobody is watching
    wait_for(lambda: recording_file_exists(recorder))
    time.sleep(1.5)
    recording = recorder.stop()
    assert not recorder.recording and not source.running  # and closed again
    path = Path(recording.path)
    assert decodable_frames(path) == recording.frames >= 15
    sidecar = json.loads(path.with_suffix(".json").read_text())
    assert sidecar["complete"] is True and sidecar["reason"] == "test" and sidecar["stopped_because"] == STOPPED
    assert sidecar["width"] == SIZE[0] and "clock_synced" in sidecar and sidecar["boot_id"] == recorder.store.boot_id
    assert saved == [recording]
    assert notes.sent[-1][0] == "info" and notes.sent[-1][1].startswith(f"Recording saved: {path.name}")


def test_the_sidecar_is_on_the_disk_before_its_name_is(tmp_path, monkeypatch):
    """A USB stick pulled mid-recording kept the sidecar's name but none of its bytes: the file is fsynced before the rename."""
    events = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace", lambda a, b: (events.append("replace"), real_replace(a, b))[1])
    recording = Recording(path=str(tmp_path / "rec.mp4"), reason="test", started_at="now", clock_synced=None, boot_id="b",
                          counter=1, width=SIZE[0], height=SIZE[1], fps=FPS)
    recording.write_sidecar()
    assert events[:2] == ["fsync", "replace"]  # then the folder, so the rename itself survives
    assert json.loads(recording.sidecar().read_text())["counter"] == 1
    events.clear()
    recording.write_sidecar(fsync=False)
    assert events == ["replace"]


def test_starting_twice_does_not_start_a_second_recording(source, tmp_path):
    recorder, _ = recorder_for(source, tmp_path)
    assert recorder.start() is True
    try:
        assert recorder.start() is False
    finally:
        recorder.stop()
    assert len(recorder.store.recordings()) == 1


def test_the_safety_cap_stops_a_recording_after_one_warning(source, tmp_path):
    clock = Clock()
    recorder, notes = recorder_for(source, tmp_path, max_seconds=60, warn_before_end_seconds=20, clock=clock)
    recorder.start()
    wait_for(lambda: recording_file_exists(recorder))
    clock.advance(45)  # 15 s left: inside the warning window
    wait_for(lambda: any(kind == "warning" for kind, _ in notes.sent))
    clock.advance(5)
    time.sleep(0.5)
    warnings = [text for kind, text in notes.sent if kind == "warning"]
    assert warnings == ["Recording will stop in 15 seconds. Open the camera to keep it going."]  # once, not every tick
    clock.advance(11)
    wait_for(lambda: not recorder.recording)
    assert recorder.last.stopped_because == MAX_SECONDS and recorder.last.complete


def test_extend_pushes_the_end_back_and_warns_again_before_the_new_end(source, tmp_path):
    clock = Clock()
    recorder, notes = recorder_for(source, tmp_path, max_seconds=60, warn_before_end_seconds=20, clock=clock)
    recorder.start()
    wait_for(lambda: recording_file_exists(recorder))
    clock.advance(50)
    wait_for(lambda: len([n for n in notes.sent if n[0] == "warning"]) == 1)
    assert recorder.extend() is True
    assert recorder.seconds_left() == pytest.approx(60)  # another max_seconds from NOW, not from the old end
    clock.advance(55)  # past the old end: still recording
    wait_for(lambda: len([n for n in notes.sent if n[0] == "warning"]) == 2)
    assert recorder.recording
    recording = recorder.stop()
    assert recording.extensions == 1 and recording.stopped_because == STOPPED
    assert recorder.extend() is False  # nothing is recording


def test_no_warning_when_it_would_come_before_there_is_time_to_react(source, tmp_path):
    clock = Clock()
    recorder, notes = recorder_for(source, tmp_path, max_seconds=30, warn_before_end_seconds=30, clock=clock)
    recorder.start()
    wait_for(lambda: recording_file_exists(recorder))
    clock.advance(31)
    wait_for(lambda: not recorder.recording)
    assert [kind for kind, _ in notes.sent] == ["info"]


def test_a_recording_that_cannot_start_for_lack_of_space_says_so(source, tmp_path):
    notes = Notes()
    store = RecordingStore(tmp_path, min_free_bytes=1000, free_bytes=lambda _: 10)
    recorder = Recorder(source, store=store, notify=notes, fsync=False)
    with pytest.raises(OSError):
        recorder.start()
    assert not recorder.recording and not source.running
    assert notes.sent == [("warning", "Recording did not start: the card is nearly full")]


def test_a_recording_stops_when_the_disk_fills_during_it(source, tmp_path, monkeypatch):
    from pihub.video import recording as module

    monkeypatch.setattr(module, "STORAGE_CHECK_SECONDS", 0.0)
    free = {"bytes": 10**12}
    notes = Notes()
    store = RecordingStore(tmp_path, min_free_bytes=1000, free_bytes=lambda _: free["bytes"])
    recorder = Recorder(source, store=store, notify=notes, fsync=False)
    recorder.start()
    wait_for(lambda: recording_file_exists(recorder))
    free["bytes"] = 10
    wait_for(lambda: not recorder.recording)
    assert recorder.last.stopped_because == "storage-full"
    assert Path(recorder.last.path).exists()  # the recording in progress is kept, not deleted to make room
    assert notes.sent[-1][0] == "warning" and notes.sent[-1][1].startswith("Recording stopped: the card is nearly full")


def test_a_camera_that_stops_sending_ends_the_recording_cleanly(tmp_path, monkeypatch):
    from pihub.video import recording as module

    monkeypatch.setattr(module, "PACKET_TIMEOUT_SECONDS", 0.5)
    source = EncodedTestPatternSource(size=SIZE, fps=FPS)
    recorder, notes = recorder_for(source, tmp_path)
    recorder.start()
    wait_for(lambda: recording_file_exists(recorder))
    source._running.clear()  # the encoder thread dies (a camera fault)
    wait_for(lambda: not recorder.recording)
    assert recorder.last.stopped_because == CAMERA_STOPPED and decodable_frames(Path(recorder.last.path)) > 0
    assert notes.sent[-1][0] == "warning"


def test_a_recorder_needs_an_encoded_source_and_a_sensible_pre_roll(tmp_path):
    with pytest.raises(ValueError):
        Recorder(TestPatternSource(), directory=tmp_path)
    with pytest.raises(ValueError):
        Recorder(EncodedTestPatternSource(), directory=tmp_path, pre_roll_seconds=-1)
    with pytest.raises(ValueError):
        Recorder(EncodedTestPatternSource(), directory=tmp_path, pre_roll_seconds=MAX_PRE_ROLL_SECONDS + 1)
    with pytest.raises(ValueError):
        TestPatternSource().add_packet_listener(lambda *a: None)


# --- pre-roll ---------------------------------------------------------------------------------------------------------------------


class HandFedSource:
    """An encoded source whose packets the test hands over one at a time, with a clock that moves a frame per packet."""

    encoded = True
    width, height = SIZE
    fps = FPS

    def __init__(self) -> None:
        self.users = 0
        self.listeners = []
        self.recorders = []
        self.now = 0.0
        self.packets = encoded_packets(12)  # a keyframe every FPS packets
        self.sent = 0
        self.keyframe_requests = 0

    @property
    def running(self) -> bool:
        return self.users > 0

    def start(self) -> None:
        self.users += 1

    def stop(self) -> None:
        self.users -= 1

    def add_packet_listener(self, listener) -> None:
        self.listeners.append(listener)

    def remove_packet_listener(self, listener) -> None:
        self.listeners.remove(listener)

    def request_keyframe(self) -> None:
        self.keyframe_requests += 1

    def feed(self, seconds: float) -> None:
        for _ in range(int(round(seconds * FPS))):
            data, key = self.packets[self.sent]
            for listener in list(self.listeners):
                listener(data, key, int(self.sent * 1_000_000 / FPS))
            self.sent += 1
            self.now = self.sent / FPS


def pre_roll_recorder(tmp_path, seconds=2.0):
    source = HandFedSource()
    store = RecordingStore(tmp_path, free_bytes=big_disk)
    recorder = Recorder(source, store=store, notify=Notes(), fsync=False, pre_roll_seconds=seconds, clock=lambda: source.now)
    return source, recorder


def stop_when_written(recorder):
    wait_for(lambda: recorder._packets.empty())
    time.sleep(0.3)  # the last packet taken off the queue is written
    return recorder.stop()


def test_armed_the_recording_begins_with_the_seconds_before_the_trigger_from_a_keyframe(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    recorder.arm()
    assert source.users == 1  # the camera runs while nothing records: that is what being ready costs
    source.feed(5.4)  # keyframes at 0, 1, 2, 3, 4 and 5 s; the trigger at 5.4 s
    assert recorder.start(reason="motion") is True
    assert source.keyframe_requests == 0  # the ring already starts at one
    source.feed(2)
    recording = stop_when_written(recorder)
    # The ring kept from the keyframe at 3 s, the newest one at least 2 s old: 2.4 s before the trigger, then 2 s after it.
    assert recording.pre_roll_seconds == pytest.approx(2.4)
    assert recording.frames == 66 and decodable_frames(Path(recording.path)) == 66
    assert json.loads(recording.sidecar().read_text())["pre_roll_seconds"] == pytest.approx(2.4)
    assert source.users == 1  # still armed: the camera stays open for the next trigger


def test_the_ring_keeps_up_to_a_second_more_rather_than_ever_less(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    recorder.arm()
    source.feed(5)  # the last frame came at 4.93 s, when the keyframe at 3 s was 1.93 s old: too new to be the ring's start
    recorder.start()
    recording = stop_when_written(recorder)
    assert recording.pre_roll_seconds == pytest.approx(3.0)


def test_the_ring_pauses_while_a_recording_runs_and_starts_again_when_it_ends(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    recorder.arm()
    source.feed(3)
    recorder.start()
    source.feed(3.4)
    assert not recorder._ring  # those packets are being saved already
    stop_when_written(recorder)
    source.feed(0.4)  # 6.4 to 6.8 s, mid-GOP: the ring waits for the next keyframe
    assert not recorder._ring
    source.feed(3)  # keyframes at 7, 8 and 9 s; the trigger at 9.8 s
    recorder.start()
    second = stop_when_written(recorder)
    assert second.pre_roll_seconds == pytest.approx(2.8)
    assert decodable_frames(Path(second.path)) == 42


def test_with_pre_roll_off_or_not_armed_nothing_holds_the_camera(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=0)
    recorder.arm()
    assert source.users == 0 and not source.listeners
    recorder.set_pre_roll(3)  # armed: takes effect at once
    assert source.users == 1 and len(source.listeners) == 1
    recorder.start()
    assert source.users == 2 and len(source.listeners) == 1  # one listener, routing the packets
    source.feed(1)
    stop_when_written(recorder)
    assert source.users == 1
    recorder.set_pre_roll(0)
    assert source.users == 0 and not source.listeners
    recorder.set_pre_roll(2)
    recorder.disarm()
    assert source.users == 0 and not source.listeners and not recorder.armed


def test_disarming_during_a_recording_keeps_the_recording_going(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    recorder.arm()
    source.feed(3)
    recorder.start()
    recorder.disarm()
    assert source.users == 1  # the recording's own hold
    source.feed(1)
    recording = stop_when_written(recorder)
    assert recording.frames == 4 * FPS and source.users == 0 and not source.listeners


def test_closing_lets_go_of_the_camera(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    recorder.arm()
    source.feed(2)
    recorder.start()
    source.feed(1)
    recorder.close()
    assert source.users == 0 and not source.listeners


def test_unarmed_a_recording_starts_on_the_trigger_as_before(tmp_path):
    source, recorder = pre_roll_recorder(tmp_path, seconds=2)
    source.feed(2)  # nobody listening
    recorder.start()
    assert source.keyframe_requests == 1
    source.feed(2)
    recording = stop_when_written(recorder)
    assert recording.pre_roll_seconds == 0 and recording.frames == 2 * FPS


def test_a_recorder_registers_with_its_source(tmp_path):
    source = EncodedTestPatternSource()
    recorder = Recorder(source, directory=tmp_path)
    assert source.recorders == [recorder]


# --- one encode, a stream and a recording at once ------------------------------------------------------------------------------------


def test_one_encode_feeds_a_live_stream_and_a_recording_and_neither_takes_the_others_packets(source, tmp_path):
    recorder, _ = recorder_for(source, tmp_path)
    source.start()  # a viewer's stream holds the camera
    streamed = []
    stop_stream = threading.Event()

    def stream():
        while not stop_stream.is_set():
            streamed.append(bytes(source.read()))

    reader = threading.Thread(target=stream)
    reader.start()
    try:
        time.sleep(0.3)
        recorder.start()
        time.sleep(2.0)
        before = len(streamed)
        recording = recorder.stop()
        assert source.running  # the recording let go; the viewer still has the camera
        time.sleep(0.3)
        assert len(streamed) > before  # and the stream carried on
    finally:
        stop_stream.set()
        reader.join()
        source.stop()
    assert recording.frames >= 25  # about 2 s at 15 fps: the recording got a full share...
    assert len(streamed) >= recording.frames  # ...and so did the stream: every packet went to both, not split between them
    assert decodable_frames(Path(recording.path)) == recording.frames


def test_a_recording_with_nobody_watching_does_not_make_the_stream_buffer_ask_for_keyframes(source, tmp_path):
    recorder, _ = recorder_for(source, tmp_path)
    recorder.start()
    time.sleep(3.0)  # the stream's buffer (2 s) would have overflowed about once by now if nobody reading counted as overflow
    recorder.stop()
    assert source.keyframes_requested == 1  # only the recorder's own request, to start its file promptly


# --- the stream's buffer while nobody reads it -------------------------------------------------------------------------------------------


def test_a_buffer_nobody_reads_discards_quietly_and_a_returning_reader_gets_a_keyframe_at_once():
    clock = Clock()
    asked = []
    buffer = hardware.PacketBuffer(maxsize=3, on_overflow=lambda: asked.append(True), clock=clock)
    clock.advance(hardware.READER_IDLE_SECONDS + 0.1)  # the stream never started reading: a recording alone
    for n in range(10):
        buffer.put(bytes([n]), n % 5 == 0)
    assert asked == [] and buffer.dropped == 0 and buffer.discarded_idle == 10  # quiet: not an overflow

    # A viewer starts reading. The next packet is a delta frame: a keyframe is asked for, and the stream starts from it.
    result = []
    reader = threading.Thread(target=lambda: result.append(buffer.get(2)))
    reader.start()
    wait_for(lambda: buffer._readers == 1)
    buffer.put(b"delta", False)
    assert asked == [True]
    buffer.put(b"key", True)
    reader.join()
    assert result == [(b"key", True)]
