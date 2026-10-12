"""apps/motion_trap.py with `--detector both|frames`: movement seen in the camera's small pictures, not only by the PIR sensor.

`FrameMotion` is tested on its own with made-up pictures (it needs no camera). The trap is tested in --simulate mode with the pictures and the clock scripted, so the
test says exactly which picture arrives when. What only a real Pi and a real garden can show (the thresholds, how an insect looks under infrared) is not asserted here: the
cost was measured on a Pi Zero W on 2026-10-06, and the thresholds are set from the numbers each notice reports.
"""

from __future__ import annotations

from typing import List

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("gpiozero")
pytest.importorskip("av")

from apps import motion_trap  # noqa: E402
from apps.motion_trap import FrameMotion  # noqa: E402
from tests.apps.test_motion_trap import DEVICE, INFO, TRAP, Transport, command, config_home, release, wait_for  # noqa: E402,F401

HEIGHT, WIDTH = 120, 160


def still() -> "np.ndarray":
    """A quiet scene: a smooth gradient, the same every time."""
    return np.tile((np.arange(WIDTH) * 150 // WIDTH + 40).astype(np.uint8), (HEIGHT, 1))


def with_block(percent: float, brightness: int = 240) -> "np.ndarray":
    """The quiet scene with a bright block over `percent` of the picture's width (so that share of its pixels differs)."""
    picture = still()
    picture[:, : int(WIDTH * percent / 100)] = brightness
    return picture


# -- FrameMotion, on its own -----------------------------------------------------------------------------------------------------


def test_a_scene_that_does_not_change_is_never_movement():
    detector = FrameMotion(area_percent=2, seconds=1)

    assert [detector.update(still(), t * 0.25) for t in range(40)] == [False] * 40
    assert detector.changed_percent == 0


def test_the_first_picture_has_nothing_to_compare_with():
    assert FrameMotion(area_percent=1, seconds=0).update(with_block(50), 0.0) is False


def test_a_change_must_last_long_enough_and_then_it_is_confirmed():
    detector = FrameMotion(area_percent=2, seconds=1)
    detector.update(still(), 0.0)

    # The picture alternates, so every sample differs from the one before by a lot.
    results = [detector.update(with_block(30) if t % 2 else still(), 0.25 * t) for t in range(1, 8)]

    assert results == [False, False, False, False, True, True, True]  # the change began at 0.25 s; confirmed at 1.25 s
    assert detector.active
    assert detector.peak_percent >= 25


def test_one_quick_blip_is_not_movement():
    """A moth: the picture changes for one sample, then is the same again."""
    detector = FrameMotion(area_percent=2, seconds=1)
    detector.update(still(), 0.0)
    detector.update(with_block(20), 0.25)  # the blip
    results = [detector.update(with_block(20), 0.25 * t) for t in range(2, 12)]  # then nothing changes any more

    assert not any(results)


def test_a_change_smaller_than_the_area_is_ignored():
    detector = FrameMotion(area_percent=5, seconds=0)
    detector.update(still(), 0.0)

    assert detector.update(with_block(3), 0.25) is False  # 3% of the picture changed, 5% is needed
    assert detector.update(still(), 0.5) is False
    assert detector.update(with_block(30), 0.75) is True  # 30%: enough, and no time is asked for


def test_the_whole_picture_getting_brighter_is_not_movement():
    """Auto-exposure or a cloud: every pixel moves the same way, so nothing moved."""
    detector = FrameMotion(area_percent=1, seconds=0)
    detector.update(still(), 0.0)

    assert detector.update((still().astype(np.int16) + 40).clip(0, 255).astype(np.uint8), 0.25) is False
    assert detector.changed_percent == 0


def test_a_gap_in_the_change_starts_the_wait_again():
    detector = FrameMotion(area_percent=2, seconds=1)
    detector.update(still(), 0.0)
    detector.update(with_block(30), 0.25)
    detector.update(still(), 0.5)  # changed
    detector.update(still(), 0.75)  # unchanged: the run is broken
    detector.update(with_block(30), 1.0)

    assert detector.update(still(), 1.25) is False  # only 0.25 s into the new run, not 1.25 s


def test_reset_forgets_the_last_picture():
    detector = FrameMotion(area_percent=1, seconds=0)
    detector.update(still(), 0.0)
    detector.reset()

    assert detector.update(with_block(50), 0.25) is False  # the first picture again: nothing to compare
    assert not detector.active


# -- the trap -----------------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def make(tmp_path, monkeypatch):
    made = []
    # The trap's own picture thread must not sample on its own: the test calls `sample()` with the picture it wants.
    monkeypatch.setattr(motion_trap, "SAMPLES_PER_SECOND", 0.001)

    def make(*extra: str):
        transport = Transport()
        args = motion_trap.parse_args(["--simulate", "--directory", str(tmp_path / "recordings"), "--size", "320x240", "--pre-roll", "0", *extra])
        trap = motion_trap.build(args, transport=transport)
        made.append(trap)
        trap.clock = Clock()
        trap._clock = trap.clock
        trap.pictures: List["np.ndarray"] = []
        trap.source.grab_motion_frame = lambda: trap.pictures.pop(0) if trap.pictures else still()
        trap.device.start()
        trap.start()
        return trap, transport

    yield make
    for trap in made:
        release(trap)


def look(trap, picture: "np.ndarray", seconds: float = 0.25) -> None:
    """Let the clock run on, then show the trap one picture."""
    trap.clock.now += seconds
    trap.pictures.append(picture)
    trap.sample()


def move(trap, samples: int) -> None:
    for t in range(samples):
        look(trap, with_block(30) if t % 2 else still())


def test_the_pir_alone_is_still_the_default(make):
    trap, transport = make()

    assert trap.detector == "pir" and trap.frame_motion is None
    widgets = transport.programs()[INFO]
    assert motion_trap.AREA_SETTING not in widgets and motion_trap.MOVEMENT_SECONDS_SETTING not in widgets
    assert trap.sensor.when_motion is not None
    assert trap.source.users == 0  # pre-roll off and nothing recording: the camera is closed


def test_the_picture_settings_appear_in_the_video_group_with_their_defaults(make):
    trap, transport = make("--detector", "frames")

    widgets = transport.programs()[INFO]
    assert widgets.index(motion_trap.HOLD_SETTING) < widgets.index(motion_trap.AREA_SETTING) < widgets.index(motion_trap.MOVEMENT_SECONDS_SETTING)
    area = transport.widget(INFO, motion_trap.AREA_SETTING)["defaultProperties"]
    assert area["group"] == "Video" and area["value"] == motion_trap.DEFAULT_AREA_PERCENT
    assert transport.widget(INFO, motion_trap.MOVEMENT_SECONDS_SETTING)["defaultProperties"]["value"] == motion_trap.DEFAULT_MOVEMENT_SECONDS


def test_the_dashboard_can_change_the_settings(make):
    trap, _ = make("--detector", "frames")

    command(trap, INFO, motion_trap.AREA_SETTING, {"value": 7.5})
    command(trap, INFO, motion_trap.MOVEMENT_SECONDS_SETTING, {"value": 3})

    wait_for(lambda: trap.frame_motion.area_percent == 7.5 and trap.frame_motion.seconds == 3)


def test_a_value_given_on_the_command_line_wins(make):
    trap, _ = make("--detector", "frames", "--movement-area", "10", "--movement-seconds", "2")

    assert (trap.frame_motion.area_percent, trap.frame_motion.seconds) == (10, 2)


def test_frames_alone_needs_no_sensor_and_claims_no_pin(make):
    trap, _ = make("--detector", "frames")

    assert trap.sensor is None


def test_frames_start_a_recording_when_the_picture_really_moves_and_say_how_much(make):
    trap, transport = make("--detector", "frames", "--hold-seconds", "5")

    for _ in range(6):
        look(trap, still())
    assert not trap.recorder.recording

    move(trap, 8)

    assert trap.recorder.recording
    assert trap.recorder.current.reason == "motion"
    assert transport.runtime(TRAP, "motion")[-1] == motion_trap.MOTION
    notice = next(message for severity, message in transport.notices() if message.startswith("Motion detected"))
    assert "the picture changed" in notice and notice.endswith(": recording")


def test_a_blip_in_the_picture_starts_nothing(make):
    trap, _ = make("--detector", "frames")

    look(trap, still())
    look(trap, with_block(20))  # an insect, for one sample
    for _ in range(10):
        look(trap, with_block(20))

    assert not trap.recorder.recording


def test_the_recording_ends_once_the_picture_has_been_still_for_the_hold(make):
    trap, _ = make("--detector", "frames", "--hold-seconds", "1")
    move(trap, 8)
    assert trap.recorder.recording

    for _ in range(3):  # a still picture, but the hold (1 s) has not run out
        look(trap, still())
    assert trap.recorder.recording
    trap.clock.now += 5
    wait_for(lambda: not trap.recorder.recording)


def test_both_wants_the_sensor_active_to_start_but_not_to_carry_on(make):
    trap, _ = make("--detector", "both", "--hold-seconds", "5")
    wait_for(lambda: len(trap.sensor._queue.queue) == trap.sensor._queue.queue.maxlen)  # gpiozero's sampling thread has started

    move(trap, 8)  # the picture moves, the sensor says nothing: a moth in front of the lens
    assert not trap.recorder.recording

    trap.sensor.pin.drive_high()
    wait_for(lambda: trap.sensor.is_active)
    move(trap, 8)
    assert trap.recorder.recording

    trap.sensor.pin.drive_low()
    wait_for(lambda: not trap.sensor.is_active)
    move(trap, 2)  # still moving, sensor quiet: the recording is kept going by the picture
    assert trap.recorder.recording


def test_the_camera_is_held_open_while_armed_even_with_no_pre_roll_and_let_go_when_not(make):
    trap, _ = make("--detector", "frames")

    assert trap.source.users == 1
    command(trap, TRAP, "armed", {"value": False})
    wait_for(lambda: trap.source.users == 0)
    command(trap, TRAP, "armed", {"value": True})
    wait_for(lambda: trap.source.users == 1)


def test_a_disarmed_trap_ignores_the_picture(make):
    trap, _ = make("--detector", "frames")
    command(trap, TRAP, "armed", {"value": False})
    wait_for(lambda: not trap.armed.value)

    move(trap, 8)

    assert not trap.recorder.recording


def test_closing_gives_the_camera_back(make):
    trap, _ = make("--detector", "frames")
    assert trap.source.users == 1

    trap.close()

    assert trap.source.users == 0


def test_a_source_that_cannot_give_pictures_is_refused():
    class NoPictures:
        motion_frames = False

    with pytest.raises(ValueError, match="small pictures"):
        motion_trap.MotionTrap(device=None, source=NoPictures(), sensor=None, detector="frames")  # type: ignore[arg-type]


def test_the_picture_gauge_shows_how_much_of_the_picture_changed(make):
    trap, transport = make("--detector", "both")
    assert "picture-change" in transport.programs()[TRAP]

    look(trap, still())
    look(trap, with_block(30, brightness=110))  # a gentle change: a very bright block would also shift the picture's average, and so every other pixel

    assert transport.runtime(TRAP, "picture-change")[-1] == pytest.approx(21.2, abs=0.5)  # not 30: the block's leftmost columns were already close to 110
    look(trap, with_block(30, brightness=110))  # nothing changed since the last picture
    assert transport.runtime(TRAP, "picture-change")[-1] == 0


def test_with_the_picture_alone_the_sensor_status_says_it_is_not_used(make):
    trap, transport = make("--detector", "frames")

    assert transport.runtime(TRAP, "sensor")[-1] == "Not used"
