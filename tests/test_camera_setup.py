"""`pihub camera setup` / `show` and the saved camera setting (`pihub.camera_config`, `pihub.camera_setup`).

Why the setting exists: a NoIR camera is strongly pink under libcamera's default tuning (measured on a Zero W: red 5.6x green) and
near neutral (1.00) under `imx219_noir`; the camera cannot say which kind it is, so the user is asked once and it is saved.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

import numpy as np
import pytest

from pihub import camera_setup
from pihub.camera_config import load_camera_state, load_camera_tuning, mark_camera_warned, noir_tuning_for, save_camera_tuning
from pihub.camera_setup import colour_cast, describe_cast, looks_pink, run_camera_setup, run_camera_show
from pihub.cli import main
from pihub.video.sources import CameraUnavailable

PINK = (5.59, 3.21)  # measured: the Zero W's NoIR camera under the default tuning
NEUTRAL = (1.00, 1.00)  # the same camera under imx219_noir
WARM_ROOM = (1.30, 0.80)  # ordinary warm indoor light on a normal camera


class Harness:
    """Runs the setup flow with a scripted camera: `measure` answers by tuning, and every call is recorded."""

    def __init__(self, tmp_path, sensor="imx219", answers=("",), default=PINK, noir_result=NEUTRAL, tuning="imx219_noir"):
        self.path = tmp_path / "camera.json"
        self.answers = list(answers)
        self.prompts, self.lines, self.errors, self.measured = [], [], [], []
        self.sensor, self.default, self.noir_result, self.tuning = sensor, default, noir_result, tuning
        self.busy = False

    def input_fn(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def measure(self, tuning):
        self.measured.append(tuning)
        if self.busy:
            raise CameraUnavailable("could not take a picture: the camera is busy")
        return self.default if tuning == "" else self.noir_result

    def run(self, noir=None):
        return run_camera_setup(
            noir,
            input_fn=self.input_fn,
            out=self.lines.append,
            err=self.errors.append,
            config_path=self.path,
            detect=lambda: self.sensor,
            measure=self.measure,
            tuning_for=lambda sensor: self.tuning,
        )

    @property
    def said(self):
        return "\n".join(self.lines)


# -- the saved setting ---------------------------------------------------------------------------------------------


def test_the_setting_round_trips_and_a_normal_camera_is_a_saved_answer(tmp_path):
    path = tmp_path / "sub" / "camera.json"
    assert load_camera_tuning(path) is None
    save_camera_tuning("imx219_noir", path)
    assert load_camera_tuning(path) == "imx219_noir"
    save_camera_tuning(None, path)  # "asked, and it is a normal camera"
    assert load_camera_tuning(path) is None
    assert json.loads(path.read_text()) == {"tuning": None}


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"tuning": 7}', '{"tuning": ""}', '{"other": 1}'])
def test_a_missing_or_damaged_file_means_no_setting_and_never_raises(tmp_path, content):
    path = tmp_path / "camera.json"
    path.write_text(content)
    assert load_camera_tuning(path) is None
    assert load_camera_tuning(tmp_path / "absent.json") is None


def test_the_file_sits_beside_the_identity_file_and_follows_xdg_config_home(tmp_path, monkeypatch):
    from pihub.camera_config import default_camera_config_path
    from pihub.device_config import default_config_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_camera_config_path() == tmp_path / "thepihub" / "camera.json"
    assert default_camera_config_path().parent == default_config_path().parent


def test_a_noir_tuning_is_offered_only_if_libcamera_ships_one(tmp_path):
    (tmp_path / "imx219_noir.json").write_text("{}")
    assert noir_tuning_for("imx219", [str(tmp_path)]) == "imx219_noir"
    assert noir_tuning_for("IMX219 ", [str(tmp_path)]) == "imx219_noir"  # case and stray spaces do not matter
    assert noir_tuning_for("imx290", [str(tmp_path)]) is None
    assert noir_tuning_for("imx219", ["/nonexistent"]) is None


def test_warned_and_asked_are_different_things_and_answering_keeps_the_record_of_the_warning(tmp_path):
    path = tmp_path / "camera.json"
    state = load_camera_state(path)
    assert (state.asked, state.warned, state.tuning) == (False, False, None)
    assert mark_camera_warned(path)
    state = load_camera_state(path)
    assert (state.asked, state.warned) == (False, True)  # told once, but the question is still open
    save_camera_tuning("imx219_noir", path)
    state = load_camera_state(path)
    assert (state.asked, state.warned, state.tuning) == (True, True, "imx219_noir")
    save_camera_tuning(None, path)  # "normal": still an answer
    assert load_camera_state(path).asked and load_camera_state(path).warned


def test_show_does_not_call_a_merely_warned_camera_a_normal_one(tmp_path):
    path, lines = tmp_path / "camera.json", []
    mark_camera_warned(path)
    run_camera_show(out=lines.append, config_path=path)
    assert "not set" in lines[-1] and "normal camera" not in lines[-1]


def test_an_unwritable_folder_reports_the_warning_as_not_saved(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("x")
    assert mark_camera_warned(blocked / "camera.json") is False


# -- the colour maths ----------------------------------------------------------------------------------------------


def test_the_cast_reads_picamera2s_blue_green_red_byte_order():
    pixels = np.zeros((4, 4, 3), dtype="uint8")
    pixels[..., 0], pixels[..., 1], pixels[..., 2] = 50, 100, 200  # bytes are B, G, R
    red_over_green, blue_over_green = colour_cast(pixels)
    assert red_over_green == pytest.approx(2.0) and blue_over_green == pytest.approx(0.5)


def test_pink_is_named_only_when_it_is_strong_and_a_warm_room_is_not_pink():
    assert looks_pink(*PINK) and describe_cast(*PINK) == "strongly pink"
    assert not looks_pink(*NEUTRAL) and describe_cast(*NEUTRAL) == "close to neutral"
    assert not looks_pink(*WARM_ROOM) and describe_cast(*WARM_ROOM) == "tinted"
    assert not looks_pink(5.0, 1.0)  # red alone, with no blue lift, is not the NoIR pink


def test_a_black_picture_does_not_divide_by_zero():
    assert colour_cast(np.zeros((2, 2, 3), dtype="uint8")) == (0.0, 0.0)


# -- the flow ------------------------------------------------------------------------------------------------------


def test_a_pink_camera_defaults_to_yes_and_the_result_is_checked_and_saved(tmp_path):
    h = Harness(tmp_path, answers=[""])  # just pressing Enter
    assert h.run() == 0
    assert h.measured == ["", "imx219_noir"]  # looked with the camera's own default, then again with the NoIR tuning
    assert h.prompts == ["Is this a NoIR camera? [Y/n] "]
    assert "strongly pink" in h.said and "close to neutral" in h.said
    assert load_camera_tuning(h.path) == "imx219_noir"
    assert not h.errors


def test_a_pink_camera_can_still_be_declared_normal(tmp_path):
    h = Harness(tmp_path, answers=["n"])
    assert h.run() == 0
    assert h.measured == [""]  # no second picture is needed
    assert load_camera_tuning(h.path) is None and h.path.exists()  # and the answer is remembered


def test_a_neutral_camera_defaults_to_no_and_says_so_plainly(tmp_path):
    h = Harness(tmp_path, answers=[""], default=NEUTRAL)
    assert h.run() == 0
    assert h.prompts == ["Is this a NoIR camera (no infrared filter, 'NoIR' on the box)? [y/N] "]
    assert load_camera_tuning(h.path) is None and h.path.exists()


def test_a_neutral_looking_camera_can_still_be_declared_noir(tmp_path):
    """E.g. it is looked at under infrared lighting, where the picture is grey whatever the tuning."""
    h = Harness(tmp_path, answers=["yes"], default=NEUTRAL)
    assert h.run() == 0
    assert load_camera_tuning(h.path) == "imx219_noir"


def test_saying_noir_on_the_command_line_asks_and_measures_nothing_first(tmp_path):
    h = Harness(tmp_path)
    assert h.run(noir=True) == 0
    assert h.prompts == [] and h.measured == ["imx219_noir"]  # only the check of the result
    assert load_camera_tuning(h.path) == "imx219_noir"


def test_saying_normal_on_the_command_line_takes_no_picture_and_undoes_a_saved_noir(tmp_path):
    h = Harness(tmp_path)
    save_camera_tuning("imx219_noir", h.path)
    assert h.run(noir=False) == 0
    assert h.prompts == [] and h.measured == []
    assert load_camera_tuning(h.path) is None


def test_a_result_that_is_still_pink_is_saved_but_the_user_is_warned(tmp_path):
    h = Harness(tmp_path, answers=["y"], noir_result=PINK)
    assert h.run() == 0
    assert "still strongly pink" in h.said and "--normal" in h.said
    assert load_camera_tuning(h.path) == "imx219_noir"


def test_a_sensor_libcamera_already_knows_as_noir_needs_nothing(tmp_path):
    h = Harness(tmp_path, sensor="imx708_noir")
    assert h.run() == 0
    assert h.prompts == [] and h.measured == []
    assert "already recognises" in h.said


def test_a_sensor_with_no_noir_tuning_is_not_asked_a_question_it_cannot_act_on(tmp_path):
    h = Harness(tmp_path, sensor="imx290", tuning=None)
    assert h.run() == 0
    assert h.prompts == [] and h.measured == []
    assert "no NoIR tuning" in h.said and load_camera_tuning(h.path) is None


def test_no_camera_is_an_error_that_saves_nothing(tmp_path):
    h = Harness(tmp_path, sensor=None)
    assert h.run() == 1
    assert "no camera found" in h.errors[0] and not h.path.exists()


def test_a_busy_camera_is_an_error_that_says_what_to_stop_and_saves_nothing(tmp_path):
    h = Harness(tmp_path)
    h.busy = True
    assert h.run() == 1
    assert "busy" in h.errors[0] and "stop it" in h.errors[0] and not h.path.exists()


def test_a_camera_that_goes_busy_after_the_answer_still_saves_it(tmp_path):
    """`--noir` given up front: the only picture is the check of the result, and failing it must not lose the answer."""
    h = Harness(tmp_path)
    h.busy = True
    assert h.run(noir=True) == 0
    assert "Could not check" in h.said and load_camera_tuning(h.path) == "imx219_noir"


def test_closing_the_input_cancels_and_saves_nothing(tmp_path):
    h = Harness(tmp_path, answers=[])  # EOF at the question
    assert h.run() == 1
    assert "Nothing was saved" in h.said and not h.path.exists()


def test_a_failure_looking_for_cameras_is_reported_not_raised(tmp_path):
    h = Harness(tmp_path)
    errors = []
    code = run_camera_setup(None, out=h.lines.append, err=errors.append, config_path=h.path, detect=lambda: 1 / 0)
    assert code == 1 and "could not look for a camera" in errors[0]


# -- `show` and the command line -----------------------------------------------------------------------------------


def test_show_says_which_of_the_three_states_it_is_in(tmp_path):
    path, lines = tmp_path / "camera.json", []
    run_camera_show(out=lines.append, config_path=path)
    assert "not set" in lines[-1]
    save_camera_tuning(None, path)
    run_camera_show(out=lines.append, config_path=path)
    assert "a normal camera" in lines[-1]
    save_camera_tuning("imx219_noir", path)
    run_camera_show(out=lines.append, config_path=path)
    assert "imx219_noir" in lines[-1]


def test_the_command_line_reaches_setup_and_show(tmp_path):
    h = Harness(tmp_path)
    injected = dict(
        out=h.lines.append, err=h.errors.append, config_path=h.path, detect=lambda: "imx219", measure=h.measure,
        tuning_for=lambda sensor: "imx219_noir", input_fn=h.input_fn,
    )
    assert main(["camera", "setup", "--noir"], **injected) == 0
    assert load_camera_tuning(h.path) == "imx219_noir"
    assert main(["camera", "show"], **injected) == 0
    assert "imx219_noir" in h.lines[-1]
    assert main(["camera", "setup", "--normal"], **injected) == 0
    assert load_camera_tuning(h.path) is None


def test_noir_and_normal_cannot_be_given_together(tmp_path, capsys):
    with pytest.raises(SystemExit):
        main(["camera", "setup", "--noir", "--normal"])
    assert "not allowed with" in capsys.readouterr().err


def test_measuring_passes_the_tuning_to_the_camera_and_reads_the_cast(monkeypatch):
    """`measure_cast` with a fake camera: the tuning reaches `new_camera`, and the answer is the picture's cast."""
    seen = {}

    class FakeCamera:
        def __init__(self, tuning=None):
            seen["tuning"] = tuning

        @staticmethod
        def load_tuning_file(name, directory=None):
            return {"loaded": name}

        def create_video_configuration(self, main=None):
            seen["main"] = main
            return {}

        def configure(self, config):
            pass

        def start(self):
            pass

        def capture_array(self, name):
            pixels = np.zeros((2, 2, 3), dtype="uint8")
            pixels[..., 0], pixels[..., 1], pixels[..., 2] = 60, 100, 150
            return pixels

        def stop(self):
            seen["stopped"] = True

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(camera_setup, "import_picamera2", lambda: types.SimpleNamespace(Picamera2=FakeCamera))
    monkeypatch.setattr(camera_setup, "SETTLE_SECONDS", 0)
    assert camera_setup.measure_cast("imx219_noir") == pytest.approx((1.5, 0.6))
    assert seen["tuning"] == {"loaded": "imx219_noir.json"}
    assert seen["main"]["format"] == "RGB888"  # the byte order `colour_cast` reads
    assert seen["stopped"] and seen["closed"]  # the camera is always released


# -- asking libcamera which sensor there is: in a child process ------------------------------------------------------------------


def _fake_run(monkeypatch, *, stdout="", returncode=0, error=None):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if error is not None:
            raise error
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_the_sensor_is_asked_in_a_child_process_so_this_one_never_starts_libcamera(monkeypatch):
    # libcamera keeps the tuning it loaded when its manager started; a manager started here would ignore the NoIR tuning a stream opens with later.
    def must_not_run():
        raise AssertionError("this process must not ask libcamera")

    monkeypatch.setattr(camera_setup, "import_picamera2", must_not_run)
    calls = _fake_run(monkeypatch, stdout="imx219\n")

    assert camera_setup.detect_sensor() == "imx219"
    assert calls[0][0] == sys.executable and "_detect_sensor_here" in calls[0][-1]


def test_libcamera_logging_before_the_answer_is_ignored(monkeypatch):
    _fake_run(monkeypatch, stdout="[0:00:01.000] INFO Camera camera_manager.cpp:340 libcamera v0.7\nIMX219\n")
    assert camera_setup.detect_sensor() == "imx219"


@pytest.mark.parametrize(
    "kwargs",
    [dict(stdout=""), dict(stdout="\n"), dict(returncode=1, stdout="imx219"), dict(error=OSError("no python")), dict(error=subprocess.TimeoutExpired("x", 60))],
    ids=["no camera", "blank", "the child failed", "cannot start", "timed out"],
)
def test_no_answer_means_no_sensor_and_never_raises(monkeypatch, kwargs):
    _fake_run(monkeypatch, **kwargs)
    assert camera_setup.detect_sensor() is None


def test_the_child_process_answers_from_libcamera(monkeypatch):
    fake = types.SimpleNamespace(Picamera2=types.SimpleNamespace(global_camera_info=lambda: [{"Model": "imx219"}]))
    monkeypatch.setattr(camera_setup, "import_picamera2", lambda: fake)
    assert camera_setup._detect_sensor_here() == "imx219"
    fake.Picamera2.global_camera_info = lambda: []
    assert camera_setup._detect_sensor_here() is None

