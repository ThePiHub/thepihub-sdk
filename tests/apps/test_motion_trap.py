"""apps/motion_trap.py in --simulate mode: gpiozero's mock pins and a test-pattern camera, so the whole path (the sensor, the
recording starting and stopping, the notices, the dashboard's settings) runs with no hardware. A real recording is written and read back."""

from __future__ import annotations

import json
from datetime import datetime
import os
import signal
import time
from pathlib import Path
from typing import Callable, List

import pytest

gpiozero = pytest.importorskip("gpiozero")
av = pytest.importorskip("av")

from pihub.protocol.bindings import Binding  # noqa: E402
from pihub.protocol.messages import CommandMessage  # noqa: E402

from apps import motion_trap  # noqa: E402

DEVICE = "trap-1"
INFO = "device-information"
TRAP = motion_trap.PROGRAM_ID


class Transport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def programs(self) -> dict:
        manifest = json.loads(self.sent[0])["payload"]
        return {p["programId"]: [w["widgetId"] for w in p["widgets"]] for p in manifest["programs"]}

    def widget(self, program: str, widget: str) -> dict:
        manifest = json.loads(self.sent[0])["payload"]
        return next(w for p in manifest["programs"] if p["programId"] == program for w in p["widgets"] if w["widgetId"] == widget)

    def notices(self) -> list:
        return [
            (json.loads(raw)["payload"]["severity"], json.loads(raw)["payload"]["message"])
            for raw in list(self.sent)
            if "severity" in json.loads(raw).get("payload", {})
        ]

    def runtime(self, program: str, widget: str) -> list:
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            binding = payload.get("binding", {})
            if binding.get("programId") == program and binding.get("widgetId") == widget:
                found.append(payload.get("value"))
        return found


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.02)
    assert condition(), "timed out"


@pytest.fixture(autouse=True)
def config_home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # A registered machine, as after the first run: the app never names an id, it takes the one in the saved config.
    from pihub.device_config import DeviceConfig, default_config_path, save_device_config

    save_device_config(DeviceConfig(device_id=DEVICE, device_secret="secret", server="wss://example.invalid"), default_config_path())
    return tmp_path / "config" / "thepihub"


@pytest.fixture
def make(tmp_path):
    made = []

    def make(*extra: str, start: bool = True):
        transport = Transport()
        args = motion_trap.parse_args(
            ["--simulate", "--directory", str(tmp_path / "recordings"), "--size", "320x240", *extra]
        )
        trap = motion_trap.build(args, transport=transport)
        made.append(trap)
        if start:
            trap.device.start()
            trap.start()
        return trap, transport

    yield make
    for trap in made:
        release(trap)


def release(trap) -> None:
    trap.close()
    trap.device.stop()
    for pin in (trap.sensor, trap.ir_light):
        if pin is not None:
            pin.close()


def command(trap, program: str, widget: str, values: dict) -> None:
    binding = Binding(device_id=DEVICE, program_id=program, widget_id=widget)
    trap.device.handle_incoming(CommandMessage(binding=binding, values=values).to_json())


def frames(path: Path) -> int:
    with av.open(str(path)) as container:
        return sum(1 for _ in container.decode(video=0))


def test_the_dashboard_has_the_trap_and_the_video_group(make):
    trap, transport = make()
    programs = transport.programs()

    assert set(programs[TRAP]) == {"camera", "motion", "sensor", "armed", "test-motion"}  # no picture gauge: the sensor alone is the default
    info = programs[INFO]
    # The camera's settings, then the recorder's (the Video group), then the app's own "Keep recording after motion" beside them.
    assert info.index("camera-resolution") < info.index("video-pre-roll") < info.index("video-warn-before-end") < info.index(motion_trap.HOLD_SETTING)
    hold = transport.widget(INFO, motion_trap.HOLD_SETTING)
    assert hold["defaultProperties"]["group"] == "Video" and hold["defaultProperties"]["value"] == motion_trap.DEFAULT_HOLD_SECONDS
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.WATCHING


def test_motion_records_and_tells_the_owner_then_stops_after_the_hold(make):
    trap, transport = make("--hold-seconds", "0.5")
    wait_for(lambda: len(trap.sensor._queue.queue) == trap.sensor._queue.queue.maxlen)  # gpiozero's sampling thread has started
    trap.sensor.pin.drive_high()

    wait_for(lambda: trap.recorder.recording)
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.MOTION
    wait_for(lambda: ("info", "Motion detected: recording") in transport.notices())  # sent just after start() returns
    assert trap.recorder.current.reason == "motion"

    time.sleep(1.5)  # still moving: the hold keeps being pushed back
    assert trap.recorder.recording

    trap.sensor.pin.drive_low()
    wait_for(lambda: not trap.recorder.recording)
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.WATCHING
    saved = trap.recorder.last
    assert any(severity == "info" and message.startswith(f"Recording saved: {saved.name}") for severity, message in transport.notices())
    assert frames(Path(saved.path)) == saved.frames >= 15  # about 2 s at 15 fps, and a real file


def test_the_hold_counts_from_the_last_motion(make):
    trap, _ = make("--hold-seconds", "10")
    now = [100.0]
    trap._clock = lambda: now[0]
    trap._stop.set()  # the test ticks by hand
    assert trap.trigger()
    now[0] += 8
    assert trap.trigger()  # a second movement: 10 s from here
    now[0] += 8
    trap.tick()
    assert trap.recorder.recording
    now[0] += 2.1
    trap.tick()
    assert not trap.recorder.recording


def test_the_test_motion_button_takes_the_same_path(make):
    trap, transport = make("--hold-seconds", "30")
    command(trap, TRAP, "test-motion", {"action": "press"})
    wait_for(lambda: trap.recorder.recording)
    assert trap.recorder.current.reason == "test"
    wait_for(lambda: ("info", "Test motion: recording") in transport.notices())


def test_disarmed_motion_is_ignored_and_disarming_ends_a_recording(make):
    trap, transport = make("--hold-seconds", "30")
    trap.trigger()
    assert trap.recorder.recording

    command(trap, TRAP, "armed", {"value": False})
    wait_for(lambda: not trap.recorder.recording)
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.NOT_ARMED
    assert trap.trigger() is False and not trap.recorder.recording

    command(trap, TRAP, "armed", {"value": True})
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.WATCHING


def test_the_hold_is_a_saved_setting_and_a_command_line_value_wins(make, config_home):
    trap, _ = make()
    command(trap, INFO, motion_trap.HOLD_SETTING, {"value": 45})
    assert trap.hold_seconds == 45
    assert json.loads((config_home / "settings.json").read_text())[f"{INFO}.{motion_trap.HOLD_SETTING}"] == 45
    release(trap)

    again, _ = make()
    assert again.hold_seconds == 45
    release(again)

    given, _ = make("--hold-seconds", "20")
    assert given.hold_seconds == 20


def test_pre_roll_from_the_command_line_arms_the_camera(make):
    trap, _ = make("--pre-roll", "2", "--hold-seconds", "30")
    assert trap.recorder.armed and trap.recorder.source.running  # ready: the camera runs while nothing records
    time.sleep(2.5)
    trap.trigger()
    assert trap.recorder.current.pre_roll_seconds >= 2


# -- nothing glows at the wildlife ---------------------------------------------------------------------------------------------------


def test_the_ir_light_is_on_exactly_while_the_camera_is(make):
    trap, _ = make("--hold-seconds", "30", "--pre-roll", "0")
    assert not trap.ir_light.is_active  # armed, pre-roll turned off: the camera is closed, so the light is off
    trap.trigger()
    assert trap.ir_light.is_active
    trap._end_motion()
    assert not trap.ir_light.is_active

    trap.camera.handle(type("Command", (), {"action": "snapshot"})())  # a snapshot opens the camera too, with no motion at all
    wait_for(lambda: trap.camera.state()["lastSnapshot"] is not None)
    assert not trap.ir_light.is_active


def test_pre_roll_is_on_by_default_and_keeps_the_camera_and_ir_light_on_while_armed(make):
    trap, _ = make()
    assert trap.recorder.pre_roll_seconds == motion_trap.DEFAULT_PRE_ROLL_SECONDS > 0
    assert trap.ir_light.is_active


def test_with_pre_roll_the_ir_light_stays_on_while_armed(make):
    trap, transport = make("--pre-roll", "2")
    assert trap.ir_light.is_active  # the camera runs all the time, so the light must
    command(trap, TRAP, "armed", {"value": False})
    assert not trap.ir_light.is_active


def test_there_is_no_visible_led_to_switch_on(tmp_path):
    """The setup LED is gone: nothing visible lights on motion, and the option no longer exists."""
    args = motion_trap.parse_args(["--simulate"])
    assert args.ir_pin == 22 and args.board_leds == "off" and not hasattr(args, "led_pin")
    with pytest.raises(SystemExit):
        motion_trap.parse_args(["--simulate", "--led-pin", "27"])


def test_the_board_leds_go_off_while_it_runs_and_come_back(make, tmp_path, monkeypatch):
    leds = fake_leds(tmp_path / "leds", writable=True)
    monkeypatch.setattr("pihub.board_leds.LEDS_DIRECTORY", leds)
    trap, transport = make()
    assert (leds / "ACT" / "trigger").read_text() == "none" and (leds / "ACT" / "brightness").read_text() == "0"
    assert not any("still on" in message for _, message in transport.notices())
    release(trap)
    assert (leds / "ACT" / "trigger").read_text() == "mmc0"


def test_without_permission_it_says_how_to_switch_them_off(make, tmp_path, monkeypatch):
    leds = fake_leds(tmp_path / "leds", writable=False)
    monkeypatch.setattr("pihub.board_leds.LEDS_DIRECTORY", leds)
    _, transport = make()
    [notice] = [message for _, message in transport.notices() if "still on" in message]
    assert "ACT" in notice and "sudo .venv/bin/pihub board-leds allow" in notice


def fake_leds(directory: Path, *, writable: bool) -> Path:
    led = directory / "ACT"
    led.mkdir(parents=True)
    (led / "trigger").write_text("none rc-feedback [mmc0] heartbeat\n")
    (led / "brightness").write_text("1\n")
    if not writable:
        for name in ("trigger", "brightness"):
            (led / name).chmod(0o444)
    return directory


class RealCamera(motion_trap.EncodedTestPatternSource):
    """An encoded source that claims to be a physical camera: what `best_camera_source()` returns on a Pi."""

    real_camera = True
    description = "a stand-in camera"


def test_on_a_pi_with_no_working_pin_driver_the_app_says_what_to_install(monkeypatch):
    """What a venv without Raspberry Pi OS's `lgpio` got on a Pi 3A+ (S6): gpiozero's own driver fails exporting a pin."""
    from gpiozero.pins.mock import MockFactory

    from pihub.gpio import PIN_DRIVER_HELP

    prepared = []
    monkeypatch.setattr("pihub.gpio.prepare_pins", lambda *a: prepared.append(True) or False)
    monkeypatch.setattr(motion_trap, "best_camera_source", lambda **kwargs: RealCamera(size=(640, 480), fps=15))

    def no_driver(*args, **kwargs):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(gpiozero.Device, "pin_factory", MockFactory())
    monkeypatch.setattr(gpiozero, "MotionSensor", no_driver)
    with pytest.raises(SystemExit) as exit:
        motion_trap.build(motion_trap.parse_args([]), transport=Transport())
    assert prepared == [True] and PIN_DRIVER_HELP in str(exit.value) and "Invalid argument" in str(exit.value)


def test_the_first_run_registers_by_link_code_and_takes_no_token(make, monkeypatch):
    """The newest method: the Device is built to register if needed (a link and a code, the platform assigns the id), the old
    `--register TOKEN` flag is gone, and `--server` reaches the Device."""
    real_device = motion_trap.Device
    seen = {}
    monkeypatch.setattr(motion_trap, "Device", lambda **kwargs: seen.update(kwargs) or real_device(**kwargs))

    make("--server", "http://dev.example")

    assert "register_if_needed" not in seen  # the default is automatic: it links itself at a terminal, and never waits without one
    assert seen["server"] == "http://dev.example"
    with pytest.raises(SystemExit):
        motion_trap.parse_args(["--register", "TOKEN"])


# -- the camera it films with -----------------------------------------------------------------------------------------


def _real_pi_build(monkeypatch, chooser, *extra):
    """Build the app the way a Pi does (not --simulate), with the pins mocked, and say what camera the chooser gives it."""
    from gpiozero.pins.mock import MockFactory

    monkeypatch.setattr("pihub.gpio.prepare_pins", lambda *a: False)
    monkeypatch.setattr(gpiozero.Device, "pin_factory", MockFactory())
    monkeypatch.setattr(motion_trap, "best_camera_source", chooser)
    return motion_trap.build(motion_trap.parse_args([*extra]), transport=Transport())


def test_it_asks_for_an_encoded_source_and_passes_software_encoder_on(monkeypatch, capsys):
    """A Pi 5 has no hardware encoder: the CPU-encoded source is still ENCODED, so the trap can record on it."""
    asked = {}

    def chooser(**kwargs):
        asked.update(kwargs)
        return RealCamera(size=(640, 480), fps=15)

    _real_pi_build(monkeypatch, chooser, "--software-encoder")

    assert asked["require_encoded"] is True
    assert asked["software_encoder"] is True
    assert "video: a stand-in camera" in capsys.readouterr().out


def test_it_will_not_film_a_test_pattern_on_a_machine_with_no_camera(monkeypatch):
    with pytest.raises(SystemExit) as exit:
        _real_pi_build(monkeypatch, lambda **kwargs: motion_trap.EncodedTestPatternSource(size=(640, 480), fps=15))
    assert "no camera here" in str(exit.value) and "--simulate" in str(exit.value)


def test_a_pi_that_cannot_record_says_what_to_install(monkeypatch):
    from pihub.video import CameraUnavailable

    def chooser(**kwargs):
        raise CameraUnavailable("recording needs an H.264 encoder: sudo apt install python3-av")

    with pytest.raises(SystemExit) as exit:
        _real_pi_build(monkeypatch, chooser)
    assert "python3-av" in str(exit.value)



def test_a_service_stop_runs_the_same_cleanup_as_ctrl_c(monkeypatch):
    """systemd stops a program with SIGTERM: the trap must still be closed (recording finished, IR light off, board LEDs restored)."""
    calls: List[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def __getattr__(self, method: str):
            return lambda *args, **kwargs: calls.append(f"{self.name}.{method}")

    trap = Recorder("trap")
    trap.device = Recorder("device")
    monkeypatch.setattr(motion_trap, "build", lambda args: trap)
    monkeypatch.setattr(motion_trap.time, "sleep", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    previous = signal.getsignal(signal.SIGTERM)
    try:
        motion_trap.main(["--simulate"])
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert calls.index("trap.close") < calls.index("device.stop")


def test_the_sensor_status_follows_the_sensors_output(make):
    trap, transport = make()
    wait_for(lambda: len(trap.sensor._queue.queue) == trap.sensor._queue.queue.maxlen)
    assert transport.runtime(TRAP, "sensor")[-1] == "Quiet"

    trap.sensor.pin.drive_high()
    wait_for(lambda: transport.runtime(TRAP, "sensor")[-1] == "Active")
    trap.sensor.pin.drive_low()
    wait_for(lambda: transport.runtime(TRAP, "sensor")[-1] == "Quiet")


# -- a brighter picture in the dark: the camera's own controls, set when it opens, and switched by its lux reading -----------------


class FakeCamera:
    """What the tuning uses of a `Picamera2`: the controls it offers, `set_controls`, whether it is running, and its metadata (one dict per reading)."""

    def __init__(self, controls=None, started: bool = True, lux=(0.3,)) -> None:
        self.camera_controls = controls if controls is not None else {
            "ExposureValue": (-8.0, 8.0, 0.0), "AeExposureMode": (0, 3, 0), "Brightness": (-1.0, 1.0, 0.0),
        }
        self.started = started
        self.applied: dict = {}
        self.lux = list(lux)  # one reading per capture_metadata; the last repeats
        self.fail_next = 0

    def set_controls(self, controls: dict) -> None:
        self.applied.update(controls)

    def capture_metadata(self) -> dict:
        if self.fail_next:
            self.fail_next -= 1
            raise RuntimeError("the camera is busy")
        lux = self.lux.pop(0) if len(self.lux) > 1 else self.lux[0]
        return {"ExposureTime": 66000, "AnalogueGain": 10.6, "DigitalGain": 1.0, "FrameDuration": 66666, "Lux": lux}


def watch(camera, switch=None, apply=None, log=None, **options):
    """Run the reading thread's loop here, with no waiting, and return what `apply` was called with."""
    calls = []
    motion_trap.watch_camera(camera, log, switch, apply or calls.append, every=0, first_after=0, **options)
    return calls


class FakeThread:
    """Stands in for threading.Thread so the hook's thread is recorded, not started (it would sleep for real)."""

    started: list = []

    def __init__(self, target, args=(), daemon=None) -> None:
        self.target, self.args = target, args

    def start(self) -> None:
        FakeThread.started.append(self)


@pytest.fixture
def threads(monkeypatch):
    FakeThread.started = []
    monkeypatch.setattr(motion_trap.threading, "Thread", FakeThread)
    return FakeThread.started


def test_night_begins_below_the_threshold_only_after_two_readings_in_a_row():
    switch = motion_trap.NightSwitch(3)
    assert switch.update(2.0) is False  # one dark reading: a torch, a car
    assert switch.update(2.0) is True
    assert motion_trap.NightSwitch(3).update(3.0) is False  # at the threshold is not below it


def test_a_single_reading_the_other_way_resets_the_count():
    switch = motion_trap.NightSwitch(3)
    switch.update(1.0)
    switch.update(50.0)  # headlights: the dark streak starts over
    assert switch.update(1.0) is False
    assert switch.update(1.0) is True


def test_night_ends_only_at_twice_the_threshold_so_a_reading_near_it_cannot_flicker():
    switch = motion_trap.NightSwitch(3)
    switch.update(1.0), switch.update(1.0)
    assert switch.night
    for lux in (3.0, 4.0, 5.9, 3.5, 5.9):  # brighter than the threshold, dimmer than twice it: still night
        assert switch.update(lux) is True
    assert switch.update(6.0) is True  # one reading is not enough
    assert switch.update(6.0) is False  # twice the threshold, twice in a row: day


def test_the_first_reading_after_the_camera_opens_acts_at_once():
    assert motion_trap.NightSwitch(3).update(0.4, first=True) is True
    night = motion_trap.NightSwitch(3)
    night.night = True
    assert night.update(900.0, first=True) is False


def test_a_reading_with_no_lux_changes_nothing():
    switch = motion_trap.NightSwitch(3)
    switch.night = True
    assert switch.update(None) is True and switch.update(None, first=True) is True


def test_nothing_asked_for_leaves_the_camera_alone():
    assert motion_trap.camera_tuning() is None
    assert motion_trap.camera_tuning(long_exposure=False) is None


def test_a_night_threshold_with_nothing_to_switch_is_an_error():
    with pytest.raises(ValueError, match="night_lux needs"):
        motion_trap.camera_tuning(night_lux=3)


def test_without_a_night_threshold_the_controls_are_always_set_and_no_thread_is_needed(threads):
    camera = FakeCamera()
    motion_trap.camera_tuning(exposure_value=1.5, long_exposure=True, brightness=0.1)(camera)

    assert camera.applied == {"ExposureValue": 1.5, "AeExposureMode": motion_trap.AE_EXPOSURE_MODE_LONG, "Brightness": 0.1}
    assert threads == []


def test_a_value_the_camera_cannot_take_is_held_to_its_range():
    camera = FakeCamera()
    motion_trap.camera_tuning(exposure_value=50, brightness=-3)(camera)

    assert camera.applied == {"ExposureValue": 8.0, "Brightness": -1.0}


def test_a_control_the_camera_does_not_have_is_skipped_not_fatal(caplog):
    camera = FakeCamera(controls={"Brightness": (-1.0, 1.0, 0.0)})
    motion_trap.camera_tuning(exposure_value=1, long_exposure=True, brightness=0.2)(camera)  # must not raise: the trap has to start

    assert camera.applied == {"Brightness": 0.2}
    assert "ExposureValue" in caplog.text and "AeExposureMode" in caplog.text


def test_with_a_night_threshold_a_new_camera_starts_as_day_and_a_thread_reads_it(threads):
    camera = FakeCamera()
    motion_trap.camera_tuning(exposure_value=2, brightness=0.1, night_lux=3)(camera)

    assert camera.applied == {}  # nothing is lifted until it is dark
    assert len(threads) == 1 and threads[0].target is motion_trap.watch_camera and threads[0].args[0] is camera


def test_a_camera_that_opens_at_night_has_the_night_controls_from_its_first_frame(threads):
    hook = motion_trap.camera_tuning(exposure_value=2, brightness=0.1, night_lux=3)
    first = FakeCamera()
    hook(first)
    _, _, switch, _ = threads[0].args
    switch.night = True  # it got dark while the first camera was open

    second = FakeCamera()
    hook(second)
    assert second.applied == {"ExposureValue": 2, "Brightness": 0.1}


def test_going_dark_applies_the_night_controls_and_daylight_puts_the_cameras_own_back(threads):
    camera = FakeCamera(lux=[500, 0.5, 0.5, 40, 40])
    motion_trap.camera_tuning(exposure_value=2, long_exposure=True, brightness=0.1, night_lux=3)(camera)
    _, log, switch, apply = threads[0].args

    watch(camera, switch, apply, samples=1)  # a bright first reading: still day
    assert camera.applied == {}
    watch(camera, switch, apply, samples=2)  # dark twice: night
    assert camera.applied == {"ExposureValue": 2, "AeExposureMode": motion_trap.AE_EXPOSURE_MODE_LONG, "Brightness": 0.1}
    watch(camera, switch, apply, samples=2)  # bright twice: the camera's own defaults again
    assert camera.applied == {"ExposureValue": 0.0, "AeExposureMode": 0, "Brightness": 0.0}


def test_the_first_reading_switches_at_once_and_a_busy_camera_does_not_stop_the_watch():
    camera = FakeCamera(lux=[0.2])
    camera.fail_next = 1  # the first reading fails: skipped, and the next is still "the first"
    switch = motion_trap.NightSwitch(3)

    assert watch(camera, switch, samples=2) == [True]


def test_the_watch_ends_when_the_camera_closes_and_reads_nothing(tmp_path):
    log = tmp_path / "camera.log"
    assert watch(FakeCamera(started=False), motion_trap.NightSwitch(3), log=str(log)) == []
    assert not log.exists()


def test_the_log_has_the_exposure_gain_lux_and_the_mode(tmp_path):
    log = tmp_path / "camera.log"
    watch(FakeCamera(lux=[500, 0.3, 0.3]), motion_trap.NightSwitch(3), log=str(log), samples=3)

    lines = log.read_text().splitlines()
    assert len(lines) == 3
    assert "exposure 66000 us" in lines[1] and "analogue gain 10.6" in lines[1] and "lux 0.3" in lines[1]
    assert [line.rsplit(", ", 1)[1] for line in lines] == ["day", "day", "night"]  # night on the second dark reading in a row


def test_the_log_without_a_switch_has_no_mode(tmp_path):
    log = tmp_path / "camera.log"
    watch(FakeCamera(), log=str(log), samples=1)
    assert log.read_text().rstrip().endswith("lux 0.3")


def test_a_log_that_cannot_be_written_does_not_stop_the_switching(tmp_path):
    calls = watch(FakeCamera(lux=[0.2]), motion_trap.NightSwitch(3), log=str(tmp_path), samples=2)  # a directory: opening it fails
    assert calls == [True]


def test_the_flags_reach_a_real_cameras_source_and_only_when_asked(tmp_path, monkeypatch):
    from gpiozero import Device as Pins
    from gpiozero.pins.mock import MockFactory

    import pihub.gpio

    monkeypatch.setattr(Pins, "pin_factory", MockFactory())
    monkeypatch.setattr(pihub.gpio, "prepare_pins", lambda: None)

    def build(*flags: str):
        """The source `build` made for a "real" camera, with the trap's pins freed again so the next call can claim them."""
        source = motion_trap.EncodedTestPatternSource(size=(320, 240))
        source.real_camera = True  # stands in for a Pi camera, which a test machine has none of
        monkeypatch.setattr(motion_trap, "best_camera_source", lambda **_: source)
        args = motion_trap.parse_args(["--directory", str(tmp_path / "recordings"), "--size", "320x240", *flags])
        trap = motion_trap.build(args, transport=Transport())
        for pin in (trap.sensor, trap.ir_light):
            if pin is not None:
                pin.close()
        return source

    assert getattr(build(), "on_open", None) is None
    assert callable(build("--exposure-value", "1", "--long-exposure", "--brightness", "0.1").on_open)
    assert callable(build("--exposure-value", "2", "--night-lux", "3").on_open)
    assert callable(build("--camera-log", str(tmp_path / "camera.log")).on_open)
    with pytest.raises(SystemExit, match="night_lux needs"):
        build("--night-lux", "3")  # a threshold with nothing to switch is refused, not silently ignored


# -- working out afterwards what it saw: the event log, the interval snapshots and the snapshot at each trigger -------------------


@pytest.fixture
def photos(tmp_path):
    return tmp_path / "photos"


@pytest.fixture
def make_logged(make, tmp_path, photos):
    """A trap whose Photos folder is a temporary one, not the home directory's, started with its event log at `tmp_path/trap.log`."""

    def make_logged(*extra: str, start: bool = True):
        trap, transport = make("--camera-log", str(tmp_path / "trap.log"), *extra, start=False)
        trap.camera.snapshot_directory = photos
        trap.device.start()  # the device is needed to say anything; the trap's own threads only when `start`
        if start:
            trap.start()
        return trap, transport

    return make_logged


def events(tmp_path) -> str:
    path = tmp_path / "trap.log"
    return path.read_text() if path.exists() else ""


def arm_pins(trap) -> None:
    wait_for(lambda: len(trap.sensor._queue.queue) == trap.sensor._queue.queue.maxlen)


def test_the_log_says_how_the_trap_started_and_each_change_of_the_sensor(make_logged, tmp_path):
    trap, _ = make_logged("--detector", "both")
    arm_pins(trap)
    assert "event: started, detector both (area 2%, must last 1 s), armed" in events(tmp_path)

    trap.sensor.pin.drive_high()
    wait_for(lambda: "event: sensor active" in events(tmp_path))
    trap.sensor.pin.drive_low()
    wait_for(lambda: "event: sensor quiet" in events(tmp_path))
    line = next(line for line in events(tmp_path).splitlines() if "sensor active" in line)
    assert datetime.fromisoformat(line.split()[0]).tzinfo is not None  # a time with its zone, to the millisecond


def test_a_trigger_is_logged_with_its_cause_and_its_end(make_logged, tmp_path):
    trap, _ = make_logged("--hold-seconds", "0.5")
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: "event: trigger (sensor): recording" in events(tmp_path))
    trap.sensor.pin.drive_low()
    wait_for(lambda: "event: motion ended" in events(tmp_path))


def test_a_snapshot_is_saved_at_each_trigger_and_named_in_the_log(make_logged, tmp_path, photos):
    trap, _ = make_logged("--hold-seconds", "0.5")
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: list(photos.glob("trigger-*.jpg")))
    (picture,) = photos.glob("trigger-*.jpg")
    assert picture.stat().st_size > 0
    wait_for(lambda: f"event: snapshot {picture.name}" in events(tmp_path))


def test_the_snapshot_at_a_trigger_can_be_turned_off(make_logged, tmp_path, photos):
    trap, _ = make_logged("--no-trigger-snapshot", "--hold-seconds", "0.5")
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: "event: trigger" in events(tmp_path))
    time.sleep(0.5)
    assert not list(photos.glob("*.jpg"))


def test_the_test_motion_button_takes_a_snapshot_too(make_logged, tmp_path, photos):
    trap, _ = make_logged("--hold-seconds", "0.5")
    trap.trigger("button")
    wait_for(lambda: list(photos.glob("trigger-*.jpg")))


def test_interval_snapshots_go_on_while_disarmed_and_the_log_says_so(make_logged, tmp_path, photos, monkeypatch):
    """A trap switched off by mistake is exactly when the pictures are wanted: they must not stop with Armed."""
    monkeypatch.setattr(motion_trap, "INTERVAL_FIRST_SECONDS", 0.05)
    trap, _ = make_logged("--snapshot-every", "0.01")  # 0.6 s
    wait_for(lambda: len(list(photos.glob("interval-*.jpg"))) >= 2, seconds=10)

    command(trap, TRAP, "armed", {"value": False})
    time.sleep(0.3)  # one that was already under way finishes
    count = len(list(photos.glob("interval-*.jpg")))
    wait_for(lambda: len(list(photos.glob("interval-*.jpg"))) >= count + 2, seconds=10)
    assert "(not armed)" in events(tmp_path) and not trap.recorder.recording


def test_there_are_no_interval_snapshots_unless_asked_for(make_logged, photos, monkeypatch):
    monkeypatch.setattr(motion_trap, "INTERVAL_FIRST_SECONDS", 0.05)
    make_logged()
    time.sleep(0.5)
    assert not list(photos.glob("interval-*.jpg"))


def test_only_the_newest_pictures_of_each_kind_are_kept(make_logged, photos):
    trap, _ = make_logged("--snapshot-keep", "2", start=False)
    for _ in range(4):
        trap._snapshot(motion_trap.INTERVAL_PREFIX)
        time.sleep(0.01)
    trap._snapshot(motion_trap.TRIGGER_PREFIX)
    kept = sorted(photos.glob("interval-*.jpg"), key=lambda found: found.stat().st_mtime_ns)
    assert len(kept) == 2 and len(list(photos.glob("trigger-*.jpg"))) == 1  # the other kind is counted on its own
    trap._snapshot(motion_trap.INTERVAL_PREFIX)
    newest = max(photos.glob("interval-*.jpg"), key=lambda found: found.stat().st_mtime_ns)
    assert newest.exists() and len(list(photos.glob("interval-*.jpg"))) == 2


def test_a_snapshot_that_cannot_be_taken_is_logged_and_does_not_stop_the_trap(make_logged, tmp_path, monkeypatch):
    def broken(*_args, **_kwargs):
        raise motion_trap.CameraUnavailable("the camera is busy")

    trap, _ = make_logged("--hold-seconds", "0.5")
    monkeypatch.setattr(motion_trap, "capture_still", broken)
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: "snapshot failed: the camera is busy" in events(tmp_path))
    assert trap.recorder.recording  # the recording went on


class PictureSequence:
    """What `sample` reads: a flat picture and one with a third of it bright, in the order the test gives."""

    def __init__(self, order: str) -> None:
        import numpy as np

        flat = np.zeros((60, 80), dtype="uint8")
        busy = flat.copy()
        busy[:, :30] = 220
        self.pictures = [busy if letter == "b" else flat for letter in order]

    def __call__(self):
        return self.pictures.pop(0)


def run_samples(trap, order: str, step: float) -> None:
    now = [0.0]
    trap._clock = lambda: now[0]
    trap.source.grab_motion_frame = PictureSequence(order)
    for _ in order:
        trap.sample()
        now[0] += step


def test_a_picture_change_that_lasted_is_logged_with_its_size_and_whether_it_counted(make_logged, tmp_path):
    trap, _ = make_logged("--detector", "both", start=False)
    run_samples(trap, "ffbfbfff", step=0.25)  # the picture changes on four looks in a row (looks 3 to 6), then is still: a second, not enough with the sensor quiet
    lines = [line for line in events(tmp_path).splitlines() if "picture changed" in line]
    assert len(lines) == 1 and lines[0].endswith("event: picture changed 100% for 1.0 s, sensor quiet, not confirmed")


def test_one_look_of_change_is_noise_and_is_not_logged(make_logged, tmp_path):
    trap, _ = make_logged("--detector", "both", start=False)
    run_samples(trap, "ffbbff", step=0.25)  # only look 3 differs from the one before it
    assert "picture changed" not in events(tmp_path)


def test_a_picture_change_that_counted_says_so_and_whether_the_sensor_was_active(make_logged, tmp_path):
    trap, _ = make_logged("--detector", "both", "--movement-seconds", "0.5", start=False)
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: trap.sensor.is_active)
    run_samples(trap, "fbfbffff", step=0.5)
    assert "sensor active, confirmed" in events(tmp_path)
    assert "event: trigger (frames)" in events(tmp_path)



def test_a_log_that_cannot_be_written_does_not_stop_the_trap(make, tmp_path):
    trap, _ = make("--camera-log", str(tmp_path), "--hold-seconds", "0.5")  # a directory: opening it for appending fails
    arm_pins(trap)
    trap.sensor.pin.drive_high()
    wait_for(lambda: trap.recorder.recording)


def test_the_options_default_to_a_snapshot_at_each_trigger_and_none_on_a_timer():
    args = motion_trap.parse_args([])
    assert args.snapshot_every == 0 and not args.no_trigger_snapshot and args.snapshot_keep == motion_trap.DEFAULT_SNAPSHOT_KEEP
