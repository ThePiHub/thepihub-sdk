"""The one-time "your picture is pink" notification (`pihub.video.colour.watch_camera_colour`).

A NoIR camera is strongly pink until `pihub camera setup` is run, and a user who never runs it sees a hot-pink picture and nothing
to explain it. The first time the camera runs, one notification says what to do. It must fire once, only when it should, and
never nag: not after the user has answered, not after it has already spoken, not for a test pattern, not in the dark.
"""

from __future__ import annotations

import threading

import av
import numpy as np
import pytest

from pihub.camera_config import load_camera_state, mark_camera_warned, save_camera_tuning
from pihub.video import watch_camera_colour
from pihub.video.colour import colour_cast, frame_cast, looks_pink, pink_warning_message
from pihub.video.sources import CameraUnavailable, TestPatternSource, VideoSource


def picture(red, green, blue):
    """A 16x16 av.VideoFrame of one flat colour."""
    pixels = np.zeros((16, 16, 3), dtype="uint8")
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = red, green, blue
    return av.VideoFrame.from_ndarray(pixels, format="rgb24")


PINK = picture(200, 60, 140)  # red 3.3x green, blue 2.3x green: what the Zero W's NoIR camera gave
NEUTRAL = picture(120, 120, 118)
DARK = picture(9, 3, 6)  # pink ratios, but far too dark to mean anything
MAGENTA = picture(232, 13, 142)  # the Zero W's NoIR camera in a bright room: almost no green, yet nothing like dark


class FakeCamera(VideoSource):
    real_camera = True
    tuning = None

    def __init__(self, frame=PINK):
        super().__init__()
        self.frame = frame
        self.opened = 0
        self.grabs = 0

    def _open(self):
        self.opened += 1

    def _close(self):
        pass

    def _read(self):
        return self.frame

    def grab_still(self):
        self.grabs += 1
        if isinstance(self.frame, Exception):
            raise self.frame
        return self.frame


class Notifier:
    def __init__(self, fails=False):
        self.messages, self.fails = [], fails

    def __call__(self, message):
        if self.fails:
            raise RuntimeError("not connected")
        self.messages.append(message)


@pytest.fixture
def path(tmp_path):
    return tmp_path / "camera.json"


def watch(source, notify, path):
    """Attach the watch and return its check, which tests call directly (no thread, no waiting)."""
    return watch_camera_colour(source, notify, config_path=path, settle_seconds=0, sleep=lambda seconds: None)


# -- when it speaks ------------------------------------------------------------------------------------------------


def test_a_strongly_pink_camera_that_was_never_set_up_is_told_about_it_once(path):
    source, notify = FakeCamera(PINK), Notifier()
    check = watch(source, notify, path)
    check(source)
    assert len(notify.messages) == 1
    assert "strongly pink" in notify.messages[0] and "pihub camera setup" in notify.messages[0] and "NoIR" in notify.messages[0]
    assert load_camera_state(path).warned and not load_camera_state(path).asked  # spoken, but the question is still open
    check(source)  # the next start
    assert len(notify.messages) == 1


def test_it_does_not_repeat_after_a_restart_because_it_was_saved(path):
    mark_camera_warned(path)  # a previous run
    source, notify = FakeCamera(PINK), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == [] and source.grabs == 0  # not even a picture was taken


@pytest.mark.parametrize("answer", ["imx219_noir", None])
def test_it_never_speaks_once_the_user_has_answered_either_way(path, answer):
    save_camera_tuning(answer, path)  # "it is NoIR" or "it is a normal camera"
    source, notify = FakeCamera(PINK), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == [] and source.grabs == 0


def test_an_explicit_tuning_means_the_user_has_already_chosen(path):
    source, notify = FakeCamera(PINK), Notifier()
    source.tuning = "imx219_noir"
    watch(source, notify, path)(source)
    assert notify.messages == [] and source.grabs == 0


def test_a_test_pattern_is_not_a_camera_and_is_never_judged(path):
    source, notify = TestPatternSource(), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == []


# -- when it stays quiet, and looks again next time ----------------------------------------------------------------


@pytest.mark.parametrize("frame", [NEUTRAL, picture(150, 110, 100), DARK], ids=["neutral", "a warm room", "too dark"])
def test_a_picture_that_is_not_pink_is_left_alone_and_looked_at_again_next_start(path, frame):
    source, notify = FakeCamera(frame), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == [] and not path.exists()  # nothing recorded: it may be pink by daylight


def test_a_camera_that_stops_before_it_settles_is_not_a_verdict(path):
    source, notify = FakeCamera(CameraUnavailable("the camera is not running")), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == [] and not path.exists()


def test_a_notification_that_cannot_be_sent_is_not_recorded_as_sent(path):
    """Not connected at that moment: try again next start, do not silently lose the one warning."""
    source = FakeCamera(PINK)
    watch(source, Notifier(fails=True), path)(source)
    assert not load_camera_state(path).warned
    notify = Notifier()
    watch(source, notify, path)(source)
    assert len(notify.messages) == 1


def test_an_unwritable_config_folder_does_not_break_the_warning(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a folder")
    source, notify = FakeCamera(PINK), Notifier()
    watch(source, notify, blocked / "camera.json")(source)  # cannot save that it spoke
    assert len(notify.messages) == 1


# -- how it is triggered -------------------------------------------------------------------------------------------


def test_the_check_runs_when_the_camera_opens_and_not_for_a_second_user_sharing_it(path):
    source, notify, done = FakeCamera(PINK), Notifier(), threading.Event()
    real_notify = notify

    def signalling(message):
        real_notify(message)
        done.set()

    watch_camera_colour(source, signalling, config_path=path, settle_seconds=0, sleep=lambda seconds: None)
    source.start()  # the first user opens the camera: the check starts on a thread
    assert done.wait(5), "the check never ran"
    grabs = source.grabs
    source.start()  # a second user shares the open camera
    source.stop()
    source.stop()
    assert source.opened == 1 and source.grabs == grabs and len(notify.messages) == 1


def test_a_listener_that_raises_does_not_stop_the_camera_starting():
    source = FakeCamera()
    source.add_start_listener(lambda s: 1 / 0)
    source.start()
    assert source.running and source.opened == 1
    source.stop()


def test_a_camera_with_the_dashboard_toggle_is_pointed_at_it_not_at_the_command(path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/pi/.config-test")
    source, notify = FakeCamera(PINK), Notifier()
    source.noir_setting = True  # what `add_settings` sets once "Camera is NoIR" is on the dashboard
    watch(source, notify, path)(source)
    (message,) = notify.messages
    assert '"Camera is NoIR"' in message and "Device Information" in message and "stop and start the camera" in message
    assert "pihub camera setup" not in message and "XDG_CONFIG_HOME" not in message


def test_the_message_names_the_config_folder_only_when_one_is_set(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert "XDG_CONFIG_HOME" not in pink_warning_message()
    monkeypatch.setenv("XDG_CONFIG_HOME", "/home/pi/.config-test")
    assert "XDG_CONFIG_HOME=/home/pi/.config-test" in pink_warning_message()


# -- the colour maths on real frames -------------------------------------------------------------------------------


def test_a_frame_is_read_as_red_green_blue_not_blue_green_red():
    red_over_green, blue_over_green = frame_cast(picture(200, 100, 50))
    assert red_over_green == pytest.approx(2.0, abs=0.05) and blue_over_green == pytest.approx(0.5, abs=0.05)


def test_the_same_numbers_read_differently_from_a_picamera2_array():
    pixels = np.zeros((2, 2, 3), dtype="uint8")
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = 50, 100, 200  # picamera2's RGB888 bytes: blue, green, red
    assert colour_cast(pixels)[0] == pytest.approx(2.0)
    assert colour_cast(pixels, bgr=False)[0] == pytest.approx(0.5)


def test_a_bright_picture_with_almost_no_green_is_judged_and_is_pink_not_too_dark():
    assert frame_cast(DARK) is None  # every channel is dark
    assert looks_pink(*frame_cast(MAGENTA))


def test_a_very_pink_picture_gets_the_warning(path):
    source, notify = FakeCamera(MAGENTA), Notifier()
    watch(source, notify, path)(source)
    assert notify.messages == [pink_warning_message()]


def test_the_flat_pink_test_picture_is_pink_and_the_neutral_one_is_not():
    assert looks_pink(*frame_cast(PINK)) and not looks_pink(*frame_cast(NEUTRAL))
