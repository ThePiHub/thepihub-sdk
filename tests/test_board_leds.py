"""The Pi's own lights, off while a camera trap runs (`pihub.board_leds`), against a pretend /sys/class/leds; and the camera's power
listener, which an infrared light follows."""

from __future__ import annotations

from pathlib import Path

import pytest

from pihub.board_leds import RULE, BoardLeds, run_allow, run_status
from pihub.cli import main
from pihub.video import TestPatternSource


def fake_leds(directory: Path, leds: dict, *, writable: bool = True) -> Path:
    for name, (trigger, brightness) in leds.items():
        led = directory / name
        led.mkdir(parents=True)
        (led / "trigger").write_text(trigger + "\n")
        (led / "brightness").write_text(brightness + "\n")
        if not writable:
            (led / "trigger").chmod(0o444)
            (led / "brightness").chmod(0o444)
    return directory


PI_3A = {"ACT": ("none [mmc0] heartbeat", "0"), "PWR": ("none [default-on] mmc0", "255"), "mmc0": ("[mmc0]", "1")}


def test_off_then_back_as_they_were(tmp_path):
    leds = BoardLeds(fake_leds(tmp_path, PI_3A))
    assert leds.names() == ["ACT", "PWR"]  # the board's own, not every LED the kernel knows
    assert leds.switch_off() == []
    for name in ("ACT", "PWR"):
        assert (tmp_path / name / "trigger").read_text() == "none" and (tmp_path / name / "brightness").read_text() == "0"
    leds.restore()
    assert (tmp_path / "ACT" / "trigger").read_text() == "mmc0" and (tmp_path / "PWR" / "trigger").read_text() == "default-on"


def test_a_led_that_was_off_by_hand_is_put_back_off(tmp_path):
    leds = BoardLeds(fake_leds(tmp_path, {"ACT": ("[none] mmc0", "0")}))
    leds.switch_off()
    leds.restore()
    assert (tmp_path / "ACT" / "trigger").read_text() == "none" and (tmp_path / "ACT" / "brightness").read_text() == "0"


def test_without_permission_it_changes_nothing_and_says_which(tmp_path):
    leds = BoardLeds(fake_leds(tmp_path, PI_3A, writable=False))
    assert leds.switch_off() == ["ACT", "PWR"]
    leds.restore()  # nothing to put back, and no error
    assert "[mmc0]" in (tmp_path / "ACT" / "trigger").read_text()


def test_a_machine_without_board_leds_has_nothing_to_do(tmp_path):
    leds = BoardLeds(tmp_path / "none-here")
    assert leds.names() == [] and leds.switch_off() == []


def test_allow_needs_root_and_installs_the_udev_rule(tmp_path):
    said, ran = [], []
    rule = tmp_path / "rules.d" / "99-pihub-board-leds.rules"
    assert run_allow(out=said.append, rule_path=rule, run=ran.append, is_root=lambda: False) == 1
    assert not rule.exists() and "sudo" in said[0]

    assert main(["board-leds", "allow"], out=said.append, rule_path=rule, run=ran.append, is_root=lambda: True) == 0
    assert rule.read_text() == RULE and 'KERNEL=="ACT|PWR|led0|led1"' in RULE and "chgrp gpio" in RULE
    assert ran == [["udevadm", "control", "--reload-rules"], ["udevadm", "trigger", "--action=add", "--subsystem-match=leds"]]


def test_status_says_what_is_lit_and_who_may_switch_it(tmp_path):
    said = []
    run_status(out=said.append, directory=fake_leds(tmp_path, PI_3A, writable=False))
    assert said[0].startswith("ACT: off (trigger mmc0); root only") and said[1].startswith("PWR: on (trigger default-on)")


# -- the camera's power listener ------------------------------------------------------------------------------------------------------


def test_a_power_listener_hears_the_camera_open_and_close_once_however_many_use_it():
    source = TestPatternSource()
    heard = []
    source.add_power_listener(lambda on: heard.append((on, source.running)))
    source.start()
    source.start()  # a second user shares it: no second "on"
    source.stop()
    source.stop()
    assert heard == [(True, False), (False, False)]  # on BEFORE it opens (not yet running), off once it has closed


def test_a_camera_that_fails_to_open_turns_the_light_back_off():
    source = TestPatternSource()
    heard = []
    source.add_power_listener(heard.append)

    def broken() -> None:
        raise RuntimeError("no camera")

    source._open = broken
    with pytest.raises(RuntimeError):
        source.start()
    assert heard == [True, False] and not source.running


def test_a_broken_listener_never_stops_the_camera():
    source = TestPatternSource()
    source.add_power_listener(lambda on: 1 / 0)
    source.start()
    assert source.running
    source.stop()
