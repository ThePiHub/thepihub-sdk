"""The camera's settable components (Step C of "Sections, settable components and camera settings"): a video source describes its own
settings, `Device` adds them to Device Information, and a change is saved, kept for the next start and applied then.

Runs anywhere: a test pattern stands in for the camera, and the libcamera sensor and tuning lookups are faked.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, List

import pytest

from pihub import Device, DeviceInformation, Program
from pihub.camera_config import CameraTuningStore, load_camera_state, save_camera_tuning
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import CommandMessage
from pihub.video import HardwareCameraSource, PiCameraSource, TestPatternSource
from pihub.video.sources import CHANGE_PROMPT

INFO = "device-information"


class Transport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def severities(self) -> list:
        """(severity, message) of every notification sent."""
        return [
            (json.loads(raw)["payload"]["severity"], json.loads(raw)["payload"]["message"])
            for raw in list(self.sent)
            if "severity" in json.loads(raw).get("payload", {})
        ]

    def widgets(self) -> dict:
        manifest = json.loads(self.sent[0])["payload"]
        program = next(p for p in manifest["programs"] if p["programId"] == INFO)
        return {w["widgetId"]: w for w in program["widgets"]}

    def runtime(self, widget: str) -> list:
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            binding = payload.get("binding", {})
            if binding.get("programId") == INFO and binding.get("widgetId") == widget:
                found.append(payload.get("value"))
        return found

    def notifications(self) -> list:
        return [
            json.loads(raw)["payload"]["message"]
            for raw in list(self.sent)
            if "severity" in json.loads(raw).get("payload", {})
        ]


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch) -> Path:
    """Saved settings and camera.json go in a folder of their own, never the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "config" / "thepihub"


@pytest.fixture(autouse=True)
def fake_libcamera(monkeypatch):
    """No camera here: the sensor is an imx219, and its NoIR tuning is installed unless a test says otherwise."""
    import pihub.camera_config as camera_config
    import pihub.camera_setup as camera_setup

    monkeypatch.setattr(camera_setup, "detect_sensor", lambda: "imx219")
    monkeypatch.setattr(camera_config, "noir_tuning_for", lambda sensor, *a, **k: f"{sensor}_noir")


def started(source, *, unstarted: bool = False):
    """A started device with Device Information on and `source` as the video of a program of its own."""
    transport = Transport()
    device = Device(id="cam-1", transport=transport, device_information=DeviceInformation(sample_seconds=5))
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(source)
    device.add_program(program)
    if not unstarted:
        device.start()
    return device, transport


def change(device: Device, widget: str, value) -> None:
    binding = Binding(device_id="cam-1", program_id=INFO, widget_id=widget)
    device.handle_incoming(CommandMessage(binding=binding, values={"value": value}).to_json())


def saved(config_home: Path) -> dict:
    try:
        return json.loads((config_home / "settings.json").read_text())
    except OSError:
        return {}


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# -- what is offered -------------------------------------------------------------------------------------------------------------


def test_a_video_source_adds_resolution_and_frame_rate_to_device_information():
    _, transport = started(TestPatternSource())
    widgets = transport.widgets()

    assert widgets["camera-resolution"]["type"] == "Select"
    assert widgets["camera-frame-rate"]["type"] == "Slider"
    resolution = next(p for p in widgets["camera-resolution"]["propertySchema"] if p["name"] == "value")
    assert "640x480" in resolution["options"] and "1920x1080" in resolution["options"]
    rate = next(p for p in widgets["camera-frame-rate"]["propertySchema"] if p["name"] == "value")
    assert (rate["min"], rate["max"]) == (1, 30)
    assert widgets["camera-resolution"]["defaultProperties"]["value"] == "640x480"
    assert widgets["camera-frame-rate"]["defaultProperties"]["value"] == 15


def test_every_camera_setting_is_in_the_camera_group():
    _, transport = started(HardwareCameraSource())
    widgets = transport.widgets()

    for id in ("camera-resolution", "camera-frame-rate", "camera-bitrate", "camera-noir"):
        assert widgets[id]["defaultProperties"]["group"] == "Camera", id


def test_a_test_pattern_has_no_bitrate_and_no_noir_toggle():
    source = TestPatternSource()
    _, transport = started(source)

    assert {"camera-bitrate", "camera-noir"}.isdisjoint(transport.widgets())
    assert source.noir_setting is False  # so a pink-picture warning would not point at a toggle that is not there


def test_a_hardware_encoder_adds_a_bitrate_and_a_noir_camera_adds_its_toggle():
    source = HardwareCameraSource()
    _, transport = started(source)
    widgets = transport.widgets()
    assert source.noir_setting is True

    assert widgets["camera-bitrate"]["defaultProperties"]["value"] == 1_000_000
    assert widgets["camera-bitrate"]["defaultProperties"]["units"] == "bit/s"
    assert widgets["camera-noir"]["type"] == "Toggle"
    assert widgets["camera-noir"]["defaultProperties"]["value"] is False


def test_there_is_no_noir_toggle_when_the_sensor_has_no_noir_tuning(monkeypatch):
    import pihub.camera_config as camera_config

    monkeypatch.setattr(camera_config, "noir_tuning_for", lambda *a, **k: None)
    _, transport = started(HardwareCameraSource())

    assert "camera-noir" not in transport.widgets()


def test_there_is_no_noir_toggle_when_no_sensor_is_found(monkeypatch):
    import pihub.camera_setup as camera_setup

    monkeypatch.setattr(camera_setup, "detect_sensor", lambda: None)
    _, transport = started(HardwareCameraSource())

    assert "camera-noir" not in transport.widgets()


def test_there_is_no_noir_toggle_when_a_tuning_was_given_in_code():
    _, transport = started(HardwareCameraSource(tuning="imx219_noir"))

    assert "camera-noir" not in transport.widgets()


def test_a_software_camera_is_not_offered_sizes_it_cannot_encode():
    _, transport = started(PiCameraSource())
    resolution = next(p for p in transport.widgets()["camera-resolution"]["propertySchema"] if p["name"] == "value")

    assert resolution["options"] == ["320x240", "640x480"]


def test_a_size_given_in_code_is_offered_and_shown():
    source = TestPatternSource(size=(1000, 500), fps=45)
    _, transport = started(source)
    widgets = transport.widgets()
    resolution = next(p for p in widgets["camera-resolution"]["propertySchema"] if p["name"] == "value")
    rate = next(p for p in widgets["camera-frame-rate"]["propertySchema"] if p["name"] == "value")

    assert "1000x500" in resolution["options"]
    assert rate["max"] >= 45
    assert transport.runtime("camera-resolution")[0] == "1000x500"
    assert transport.runtime("camera-frame-rate")[0] == 45


def test_a_setting_that_was_removed_stays_removed():
    device, transport = started(TestPatternSource(), unstarted=True)
    device.information.remove("camera-frame-rate")
    device.start()

    assert "camera-frame-rate" not in transport.widgets()
    assert "camera-resolution" in transport.widgets()


def test_no_settings_without_device_information():
    transport = Transport()
    device = Device(id="cam-1", transport=transport, device_information=False)
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(TestPatternSource())
    device.add_program(program)
    device.start()

    assert INFO not in [p["programId"] for p in json.loads(transport.sent[0])["payload"]["programs"]]


# -- changing them ---------------------------------------------------------------------------------------------------------------


def test_a_change_while_the_camera_is_closed_is_in_effect_at_once_and_is_saved(config_home):
    source = TestPatternSource()
    device, transport = started(source)

    change(device, "camera-resolution", "1280x720")
    change(device, "camera-frame-rate", 24)

    assert (source.width, source.height, source.fps) == (1280, 720, 24)
    assert saved(config_home) == {f"{INFO}.camera-resolution": "1280x720", f"{INFO}.camera-frame-rate": 24}
    assert transport.runtime("camera-resolution")[-1] == "1280x720"
    assert CHANGE_PROMPT not in transport.notifications()  # nothing is running, so there is nothing to restart


def test_a_change_while_the_camera_is_open_waits_for_the_next_start_and_says_so():
    source = TestPatternSource()
    device, transport = started(source)
    source.start()

    change(device, "camera-resolution", "1280x720")

    assert (source.width, source.height) == (640, 480)  # the open camera keeps what it opened with
    assert transport.notifications() == [CHANGE_PROMPT]
    assert transport.severities() == [("warning", CHANGE_PROMPT)]  # a warning: the user has to act, so the dashboard asks for it to be acknowledged
    assert transport.runtime("camera-resolution")[-1] == "1280x720"  # but the dashboard shows what is set"

    source.stop()
    source.start()
    assert (source.width, source.height) == (1280, 720)
    source.stop()


def test_the_last_change_before_a_start_wins():
    source = TestPatternSource()
    device, _ = started(source)
    source.start()
    change(device, "camera-frame-rate", 20)
    change(device, "camera-frame-rate", 25)
    source.stop()
    source.start()

    assert source.fps == 25
    source.stop()


def test_a_value_that_is_not_valid_changes_nothing(config_home):
    source = TestPatternSource()
    device, transport = started(source)

    change(device, "camera-resolution", "9999x9999")
    change(device, "camera-frame-rate", 500)

    assert (source.width, source.height, source.fps) == (640, 480, 15)
    assert saved(config_home) == {}
    assert any("Setting not changed" in message for message in transport.notifications())


def test_a_bitrate_change_reaches_the_hardware_source():
    source = HardwareCameraSource()
    device, _ = started(source)

    change(device, "camera-bitrate", 2_000_000)

    assert source.bitrate == 2_000_000


def test_the_bitrate_slider_does_not_go_below_a_picture_you_can_use():
    from pihub.video.hardware import BITRATE_RANGE, DEFAULT_BITRATE

    assert BITRATE_RANGE[0] == 250_000 and BITRATE_RANGE[0] <= DEFAULT_BITRATE <= BITRATE_RANGE[1]
    _, transport = started(HardwareCameraSource())
    schema = next(p for p in transport.widgets()["camera-bitrate"]["propertySchema"] if p["name"] == "value")

    assert schema["min"] == 250_000 and schema["max"] == 4_000_000


def test_a_bitrate_below_the_floor_is_refused(config_home):
    source = HardwareCameraSource()
    device, transport = started(source)

    change(device, "camera-bitrate", 100_000)

    assert source.bitrate == 1_000_000
    assert saved(config_home) == {}
    assert any("Setting not changed" in message for message in transport.notifications())


def test_a_bitrate_saved_below_the_floor_by_an_earlier_version_falls_back_to_the_default(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text(json.dumps({f"{INFO}.camera-bitrate": 100_000}))
    source = HardwareCameraSource()
    started(source)

    assert source.bitrate == 1_000_000


def test_a_bitrate_given_in_code_below_the_floor_is_still_honoured():
    """A program that really wants a tiny stream can ask for one; only the dashboard slider has a floor."""
    source = HardwareCameraSource(bitrate=100_000)
    _, transport = started(source)

    assert source.bitrate == 100_000
    schema = next(p for p in transport.widgets()["camera-bitrate"]["propertySchema"] if p["name"] == "value")
    assert schema["min"] == 100_000  # the slider reaches down to what the program asked for, so it can show it


def test_a_source_refuses_a_setting_it_does_not_have():
    with pytest.raises(ValueError, match="no setting"):
        TestPatternSource().apply_setting("volume", 3)


# -- saved values, and which one wins --------------------------------------------------------------------------------------------


def test_a_saved_value_is_used_by_the_next_run(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text(json.dumps({f"{INFO}.camera-frame-rate": 24, f"{INFO}.camera-resolution": "1280x720"}))
    source = TestPatternSource()
    _, transport = started(source)

    assert (source.width, source.height, source.fps) == (1280, 720, 24)
    assert transport.runtime("camera-frame-rate")[0] == 24


def test_a_value_given_in_code_beats_a_saved_one_and_a_dashboard_change_to_it_is_not_saved(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text(json.dumps({f"{INFO}.camera-frame-rate": 24}))
    source = TestPatternSource(fps=10)
    device, transport = started(source)

    assert source.fps == 10
    assert transport.runtime("camera-frame-rate")[0] == 10

    change(device, "camera-frame-rate", 5)

    assert source.fps == 5  # for this run
    assert saved(config_home) == {f"{INFO}.camera-frame-rate": 24}  # the saved value is untouched


def test_a_saved_value_that_is_no_longer_valid_falls_back_to_the_default(config_home):
    config_home.mkdir(parents=True)
    (config_home / "settings.json").write_text(json.dumps({f"{INFO}.camera-resolution": "not a size"}))
    source = TestPatternSource()
    started(source)

    assert (source.width, source.height) == (640, 480)


# -- the NoIR toggle shares camera.json with `pihub camera setup` ------------------------------------------------------------------


def test_the_noir_toggle_saves_the_tuning_in_camera_json(config_home):
    source = HardwareCameraSource()
    device, transport = started(source)

    change(device, "camera-noir", True)

    assert load_camera_state().tuning == "imx219_noir"
    assert source.tuning is None  # "whatever is saved": the next start reads it
    assert saved(config_home) == {}  # it is not duplicated in settings.json

    change(device, "camera-noir", False)
    state = load_camera_state()
    assert state.tuning is None and state.asked  # a normal camera, and the question is answered


def test_the_noir_toggle_shows_an_answer_saved_by_setup():
    save_camera_tuning("imx219_noir")
    _, transport = started(HardwareCameraSource())

    assert transport.runtime("camera-noir")[0] is True


def test_the_noir_toggle_says_off_when_setup_was_never_run():
    _, transport = started(HardwareCameraSource())

    assert transport.runtime("camera-noir")[0] is False


def test_the_noir_store_treats_another_tuning_as_not_noir(tmp_path):
    path = tmp_path / "camera.json"
    save_camera_tuning("some_other_tuning", path)

    assert CameraTuningStore("imx219_noir", path).get("x", False) is False


def test_the_noir_change_while_the_camera_is_open_asks_for_a_restart():
    source = HardwareCameraSource()
    device, transport = started(source)
    source._users = 1  # open (no real camera here)
    source.running = True

    change(device, "camera-noir", True)

    assert CHANGE_PROMPT in transport.notifications()
    assert transport.severities() == [("warning", CHANGE_PROMPT)]
    source._users = 0
    source.running = False


# -- the recorder's settings: the "Video" group ------------------------------------------------------------------------------------


def with_recorder(tmp_path, **options):
    from pihub.video import EncodedTestPatternSource
    from pihub.video.recording import Recorder

    source = EncodedTestPatternSource()
    recorder = Recorder(source, directory=tmp_path / "recordings", fsync=False, **options)
    device, transport = started(source, unstarted=True)
    device.start()
    return device, transport, source, recorder


def test_a_recorder_adds_pre_roll_to_the_video_group(tmp_path):
    device, transport, _, recorder = with_recorder(tmp_path)
    widget = transport.widgets()["video-pre-roll"]
    value = next(p for p in widget["propertySchema"] if p["name"] == "value")

    assert widget["type"] == "Slider" and widget["defaultProperties"]["group"] == "Video"
    assert (value["min"], value["max"], value["step"]) == (0, 15, 1)
    assert widget["defaultProperties"]["value"] == 0 and recorder.pre_roll_seconds == 0
    device.stop()


def test_no_recorder_no_video_group():
    from pihub.video import EncodedTestPatternSource

    _, transport = started(EncodedTestPatternSource())
    assert "video-pre-roll" not in transport.widgets()


def test_a_pre_roll_change_is_saved_and_armed_it_holds_the_camera_at_once(tmp_path, config_home):
    device, _, source, recorder = with_recorder(tmp_path)
    recorder.arm()
    assert not source.running  # pre-roll is off: nothing held

    change(device, "video-pre-roll", 5)
    assert recorder.pre_roll_seconds == 5 and source.running
    assert saved(config_home)[f"{INFO}.video-pre-roll"] == 5

    change(device, "video-pre-roll", 0)
    assert not source.running
    recorder.close()
    device.stop()


def test_a_saved_pre_roll_is_used_after_a_restart(tmp_path, config_home):
    device, _, _, recorder = with_recorder(tmp_path)
    change(device, "video-pre-roll", 7)
    device.stop()

    device, transport, _, recorder = with_recorder(tmp_path)
    assert recorder.pre_roll_seconds == 7 and transport.runtime("video-pre-roll")[0] == 7
    device.stop()


def test_a_pre_roll_given_in_code_wins_and_is_not_saved(tmp_path, config_home):
    device, _, _, _ = with_recorder(tmp_path)
    change(device, "video-pre-roll", 7)
    device.stop()

    device, transport, _, recorder = with_recorder(tmp_path, pre_roll_seconds=3)
    assert recorder.pre_roll_seconds == 3 and transport.runtime("video-pre-roll")[0] == 3
    change(device, "video-pre-roll", 4)
    assert recorder.pre_roll_seconds == 4 and saved(config_home)[f"{INFO}.video-pre-roll"] == 7
    device.stop()


def test_the_video_group_has_the_recorders_limits_with_their_defaults(tmp_path):
    device, transport, _, recorder = with_recorder(tmp_path)
    widgets = transport.widgets()
    expected = {  # id: (default, min, max, step)
        "video-pre-roll": (0, 0, 15, 1),
        "video-max-total-mb": (4096, 256, 16384, 256),
        "video-min-free-mb": (1024, 64, 4096, 64),
        "video-max-seconds": (300, 30, 1800, 30),
        "video-warn-before-end": (30, 0, 120, 5),
    }
    for id, (default, low, high, step) in expected.items():
        value = next(p for p in widgets[id]["propertySchema"] if p["name"] == "value")
        assert widgets[id]["defaultProperties"]["group"] == "Video", id
        assert (widgets[id]["defaultProperties"]["value"], value["min"], value["max"], value["step"]) == (default, low, high, step), id
    ids = list(widgets)
    assert ids.index("video-pre-roll") < ids.index("video-max-total-mb") < ids.index("video-warn-before-end")
    device.stop()


def test_a_limit_changed_while_idle_applies_at_once_and_is_saved(tmp_path, config_home):
    device, _, _, recorder = with_recorder(tmp_path)
    change(device, "video-max-seconds", 60)
    change(device, "video-max-total-mb", 512)
    assert recorder.max_seconds == 60 and recorder.store.max_total_bytes == 512 * 1024**2
    assert saved(config_home)[f"{INFO}.video-max-seconds"] == 60
    device.stop()


def test_a_limit_changed_during_a_recording_waits_for_the_next_one(tmp_path):
    device, _, source, recorder = with_recorder(tmp_path)
    recorder.start()
    change(device, "video-max-seconds", 90)
    change(device, "video-warn-before-end", 10)
    assert recorder.max_seconds == 300 and recorder.warn_before_end_seconds == 30  # this recording keeps what it started with
    recorder.stop()
    assert recorder.max_seconds == 90 and recorder.warn_before_end_seconds == 10
    device.stop()


def test_a_limit_given_in_code_is_shown_and_wins(tmp_path, config_home):
    device, _, _, _ = with_recorder(tmp_path)
    change(device, "video-max-seconds", 60)
    device.stop()

    device, transport, _, recorder = with_recorder(tmp_path, max_seconds=10)
    value = next(p for p in transport.widgets()["video-max-seconds"]["propertySchema"] if p["name"] == "value")
    assert recorder.max_seconds == 10 and transport.runtime("video-max-seconds")[0] == 10 and value["min"] == 10
    device.stop()


# -- a camera setting changed while pre-roll holds the camera (found on a real Pi, S6) ---------------------------------------------


def armed_with_pre_roll(tmp_path):
    device, transport, source, recorder = with_recorder(tmp_path, pre_roll_seconds=5)
    recorder.arm()
    opens = []
    source.add_start_listener(lambda s: opens.append((s.width, s.height)))
    return device, transport, source, recorder, opens


def test_armed_and_idle_the_recorder_reopens_the_camera_for_a_change_and_nobody_is_asked(tmp_path):
    device, transport, source, recorder, opens = armed_with_pre_roll(tmp_path)

    change(device, "camera-resolution", "1280x720")

    assert wait_for(lambda: opens == [(1280, 720)])
    assert source.users == 1 and recorder.armed and not source.settings_pending  # held again, for the ring
    assert CHANGE_PROMPT not in transport.notifications()
    recorder.close()
    device.stop()


def test_armed_and_recording_the_change_waits_for_the_recording_to_end_then_applies(tmp_path):
    device, transport, source, recorder, opens = armed_with_pre_roll(tmp_path)
    recorder.start(reason="test")

    change(device, "camera-frame-rate", 20)

    time.sleep(0.2)
    assert opens == [] and source.fps == 15 and recorder.recording  # a change never cuts a recording short
    assert CHANGE_PROMPT not in transport.notifications()
    saved_recording = recorder.stop()
    assert saved_recording.fps == 15
    assert opens == [(640, 480)] and source.fps == 20 and source.users == 1  # reopened once it had ended
    recorder.close()
    device.stop()


def test_armed_with_a_live_stream_the_user_is_asked_and_the_change_applies_when_the_stream_stops(tmp_path):
    device, transport, source, recorder, opens = armed_with_pre_roll(tmp_path)
    source.start()  # a viewer's stream

    change(device, "camera-resolution", "1280x720")

    assert transport.notifications().count(CHANGE_PROMPT) == 1  # the stream would have to stop: that is the user's to do
    time.sleep(0.2)
    assert opens == []
    source.stop()  # the viewer stops the stream: only the ring holds the camera now
    assert wait_for(lambda: opens == [(1280, 720)])
    assert source.users == 1 and recorder.armed
    recorder.close()
    device.stop()


def test_without_pre_roll_a_recording_holding_the_camera_does_not_ask_either(tmp_path):
    device, transport, source, recorder = with_recorder(tmp_path)
    recorder.start(reason="test")

    change(device, "camera-resolution", "1280x720")

    assert CHANGE_PROMPT not in transport.notifications()  # the camera closes when the recording ends: the next start has it
    recorder.stop()
    assert source.users == 0
    source.start()
    assert (source.width, source.height) == (1280, 720)
    source.stop()
    device.stop()


def test_a_change_read_at_open_asks_only_when_nothing_will_reopen_the_camera():
    source = TestPatternSource()
    assert source.reopen_needed() is False  # closed: the next start reads it anyway
    source.start()
    assert source.reopen_needed() is True and source.settings_pending
    source.add_pending_listener(lambda: True)
    assert source.reopen_needed() is False
    source.stop()
    source.start()
    assert not source.settings_pending
    source.stop()
