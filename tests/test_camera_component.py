"""The Camera component: one widget with the video, one state value, and named
actions the SDK answers itself. A test pattern stands in for the camera; recordings and snapshots are real files."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, List

import pytest

av = pytest.importorskip("av")

from pihub import Device, Program  # noqa: E402
from pihub.video import EncodedTestPatternSource  # noqa: E402

DEVICE, PROGRAM = "cam-1", "birdbox"


class Transport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def widget(self, widget: str) -> dict:
        manifest = json.loads(self.sent[0])["payload"]
        return next(w for p in manifest["programs"] if p["programId"] == PROGRAM for w in p["widgets"] if w["widgetId"] == widget)

    def states(self) -> list:
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            if payload.get("binding", {}).get("widgetId") == "camera" and "value" in payload:
                found.append(payload["value"])
        return found

    def notices(self) -> list:
        return [
            (json.loads(raw)["payload"]["severity"], json.loads(raw)["payload"]["message"])
            for raw in list(self.sent)
            if "severity" in json.loads(raw).get("payload", {})
        ]


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    assert condition(), "timed out"


@pytest.fixture
def made(tmp_path):
    devices = []

    def make(record: bool = True, **recorder_options):
        transport = Transport()
        device = Device(id=DEVICE, transport=transport, device_information=False)
        program = Program(id=PROGRAM, name="Bird Box")
        camera = program.output(id="camera", type="Camera")
        camera.use(EncodedTestPatternSource(size=(320, 240)), snapshot_directory=str(tmp_path / "snapshots"))
        recorder = camera.recorder(directory=tmp_path / "recordings", fsync=False, **recorder_options) if record else None
        device.add_program(program)
        device.start()
        devices.append((device, recorder))
        return device, camera, recorder, transport

    yield make
    for device, recorder in devices:
        if recorder is not None:
            recorder.close()
        device.stop()


def act(device: Device, action: str) -> None:
    device.simulate_command(PROGRAM, "camera", action=action)


def test_one_widget_that_displays_and_takes_commands_and_says_what_it_offers(made):
    _, _, _, transport = made()
    widget = transport.widget("camera")
    assert widget["type"] == "Camera" and widget["capabilities"] == ["display", "command"]
    assert widget["defaultProperties"] == {"snapshot": True, "record": True}


def test_without_a_recorder_it_offers_no_record_button(made):
    _, _, _, transport = made(record=False)
    assert transport.widget("camera")["defaultProperties"] == {"snapshot": True}


def test_its_state_is_one_value_published_when_the_device_starts(made):
    _, _, _, transport = made()
    state = transport.states()[-1]
    assert state["recording"] is False and state["streamId"] is None  # no direct connections in this test: no live video
    assert set(state) >= {"streamId", "recording", "recordingStartedAt", "recordingSecondsLeft", "storageUsedPercent", "lastSnapshot"}


def test_record_from_the_dashboard_then_stop(made):
    device, _, recorder, transport = made()
    act(device, "record-start")
    wait_for(lambda: transport.states()[-1]["recording"])
    state = transport.states()[-1]
    assert state["recordingReason"] == "dashboard" and state["recordingSecondsLeft"] > 290 and state["warnBeforeEndSeconds"] == 30

    count = len(transport.states())
    time.sleep(2.2)  # while recording: once a second, so the dashboard's timer is right however late a viewer arrives
    assert len(transport.states()) >= count + 2
    assert transport.states()[-1]["recordingSeconds"] >= 2

    act(device, "record-stop")
    wait_for(lambda: not transport.states()[-1]["recording"])
    wait_for(lambda: any(message.startswith("Recording saved:") for _, message in transport.notices()))
    assert recorder.last is not None and Path(recorder.last.path).exists()
    count = len(transport.states())
    time.sleep(1.5)
    assert len(transport.states()) <= count + 1  # the ticking stops with the recording


def test_the_state_follows_a_recording_the_program_started_itself(made):
    _, _, recorder, transport = made()
    recorder.start(reason="motion")
    wait_for(lambda: transport.states()[-1]["recording"])
    assert transport.states()[-1]["recordingReason"] == "motion"
    recorder.stop()
    wait_for(lambda: not transport.states()[-1]["recording"])


def test_extend_pushes_the_end_back(made):
    device, _, recorder, transport = made(max_seconds=60, warn_before_end_seconds=30)
    act(device, "record-start")
    wait_for(lambda: recorder.recording)
    time.sleep(1.2)
    act(device, "extend")
    wait_for(lambda: transport.states()[-1]["recordingSecondsLeft"] > 59)
    assert recorder.current.extensions == 1


def test_snapshot_saves_a_picture_and_says_so(made, tmp_path):
    device, _, _, transport = made()
    act(device, "snapshot")
    wait_for(lambda: transport.states()[-1]["lastSnapshot"] is not None and not transport.states()[-1]["snapshotting"])
    name = transport.states()[-1]["lastSnapshot"]
    assert (tmp_path / "snapshots" / name).stat().st_size > 0
    assert ("info", f"Snapshot saved on the device: {name}") in transport.notices()


def test_an_unknown_action_is_ignored(made):
    device, _, recorder, transport = made()
    act(device, "self-destruct")
    assert not recorder.recording and transport.notices() == []


def test_live_video_puts_the_stream_id_in_the_state(made):
    _, camera, _, transport = made()
    camera.attach_stream(f"{PROGRAM}.camera")
    assert transport.states()[-1]["streamId"] == f"{PROGRAM}.camera"
    assert transport.states()[-1]["recording"] is False  # still the whole state, not just the stream id


def test_a_camera_is_a_video_source_for_live_video(made):
    device, camera, _, _ = made()
    assert device._programs[PROGRAM].video_sources() == {f"{PROGRAM}.camera": camera.source}


def test_recorder_needs_a_source_and_only_one():
    program = Program(id=PROGRAM, name="Bird Box")
    camera = program.output(id="camera", type="Camera")
    with pytest.raises(RuntimeError):
        camera.recorder()
    camera.use(EncodedTestPatternSource())
    camera.recorder(directory="/tmp/unused", fsync=False)
    with pytest.raises(RuntimeError):
        camera.recorder(directory="/tmp/unused", fsync=False)


# -- recorder_if_possible: the same lines on every machine ---------------------------------------------------------------


def _bare_camera(tmp_path):
    from pihub.video import TestPatternSource

    device = Device(id=DEVICE, transport=Transport())
    program = Program(id=PROGRAM, name="Bird Box")
    camera = program.output(id="camera", type="Camera")
    return program, camera, EncodedTestPatternSource(size=(160, 120), fps=5), TestPatternSource(size=(160, 120), fps=5)


def test_recorder_if_possible_adds_recording_for_an_encoded_source(tmp_path):
    _program, camera, encoded, _raw = _bare_camera(tmp_path)
    camera.use(encoded)

    recorder = camera.recorder_if_possible(directory=str(tmp_path / "recordings"))

    assert recorder is not None
    assert any(prop.name == "record" for prop in camera.properties)


def test_recorder_if_possible_does_nothing_for_a_source_that_cannot_be_recorded(tmp_path):
    """A Pi 5's camera is encoded on the CPU: raw frames, so no recording, no Record button, and no error."""
    _program, camera, _encoded, raw = _bare_camera(tmp_path)
    camera.use(raw)

    assert camera.recorder_if_possible(directory=str(tmp_path / "recordings")) is None
    assert not any(prop.name == "record" for prop in camera.properties)
    assert any(prop.name == "snapshot" for prop in camera.properties)  # snapshots still work
    with pytest.raises(ValueError, match="encoded source"):
        camera.recorder()  # the strict method still refuses


def test_recorder_if_possible_needs_a_source_first(tmp_path):
    _program, camera, _encoded, _raw = _bare_camera(tmp_path)
    with pytest.raises(RuntimeError, match=r"use\(source\)"):
        camera.recorder_if_possible()

