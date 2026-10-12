"""The Raspberry Pi's own lights (the green activity LED, and the red power LED on the boards that have one): off while a program runs.

A camera trap must not glow at the wildlife it is watching. A program asks for the lights off with `BoardLeds().switch_off()` and gets
them back as they were with `restore()`. It goes through the kernel's LED interface (`/sys/class/leds`): the trigger set to `none` and
the brightness to 0, which means "off" on every model whatever way round its LED is wired - unlike the `config.txt` settings, whose
"active low" part differs from board to board. Nothing is permanent: a reboot, or `restore()`, brings them back.

**Those files belong to root.** So, ONCE per Pi, as root:

    sudo .venv/bin/pihub board-leds allow

installs a udev rule (`/etc/udev/rules.d/99-pihub-board-leds.rules`) that gives the `gpio` group (the Pi's first user is in it) write
access to the board LEDs' trigger and brightness, now and after every boot. Without it `switch_off()` changes nothing and says why.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

LEDS_DIRECTORY = Path("/sys/class/leds")
# The board's own LEDs, by their current names and the names older kernels used.
BOARD_LED_NAMES = ("ACT", "PWR", "led0", "led1")
RULE_PATH = Path("/etc/udev/rules.d/99-pihub-board-leds.rules")
RULE = (
    "# ThePiHub (`pihub board-leds allow`): let the gpio group switch the Pi's own LEDs off, so a camera trap does not glow.\n"
    'SUBSYSTEM=="leds", KERNEL=="ACT|PWR|led0|led1", ACTION=="add", '
    'RUN+="/bin/chgrp gpio /sys%p/trigger /sys%p/brightness", RUN+="/bin/chmod g+w /sys%p/trigger /sys%p/brightness"\n'
)
ALLOW_COMMAND = "sudo .venv/bin/pihub board-leds allow"


def _current_trigger(text: str) -> str:
    """The selected trigger in a `trigger` file: the one in [brackets]."""
    for word in text.split():
        if word.startswith("[") and word.endswith("]"):
            return word[1:-1]
    return "none"


class BoardLeds:
    """The board LEDs this Pi has. `switch_off()` then `restore()`; both are safe to call on a machine with none (a PC: they do nothing)."""

    def __init__(self, directory: Union[str, Path, None] = None) -> None:
        self.directory = Path(directory) if directory is not None else LEDS_DIRECTORY
        self._saved: Dict[str, Tuple[str, str]] = {}  # name: (trigger, brightness) before switch_off

    def names(self) -> List[str]:
        return [name for name in BOARD_LED_NAMES if (self.directory / name / "brightness").exists()]

    def switch_off(self) -> List[str]:
        """Turn every board LED off. Returns the names of those that could NOT be (no permission: see `pihub board-leds allow`)."""
        refused = []
        for name in self.names():
            led = self.directory / name
            try:
                trigger = _current_trigger((led / "trigger").read_text())
                brightness = (led / "brightness").read_text().strip()
                (led / "trigger").write_text("none")
                (led / "brightness").write_text("0")
            except OSError as error:
                logger.info("board LED %s: could not switch it off (%s)", name, error.strerror or error)
                refused.append(name)
                continue
            self._saved.setdefault(name, (trigger, brightness))
        if self._saved:
            logger.info("board LEDs off: %s", ", ".join(sorted(self._saved)))
        return refused

    def restore(self) -> None:
        """Put back what `switch_off()` changed."""
        for name, (trigger, brightness) in list(self._saved.items()):
            led = self.directory / name
            try:
                (led / "trigger").write_text(trigger)
                if trigger == "none":
                    (led / "brightness").write_text(brightness)
            except OSError:
                logger.debug("board LED %s: could not restore it", name, exc_info=True)
            self._saved.pop(name, None)


def run_allow(
    *,
    out: Callable[[str], None] = print,
    rule_path: Path = RULE_PATH,
    run: Callable[[Sequence[str]], object] = lambda command: subprocess.run(command, check=False),
    is_root: Optional[Callable[[], bool]] = None,
) -> int:
    """`pihub board-leds allow`: install the udev rule and apply it now. Root only."""
    root = is_root() if is_root is not None else os.geteuid() == 0
    if not root:
        out(f"This changes a system file, so it needs root: {ALLOW_COMMAND}")
        return 1
    rule_path.parent.mkdir(parents=True, exist_ok=True)
    rule_path.write_text(RULE)
    run(["udevadm", "control", "--reload-rules"])
    run(["udevadm", "trigger", "--action=add", "--subsystem-match=leds"])
    out(f"Installed {rule_path}: programs run by members of the gpio group can now switch the Pi's own LEDs off (and back on).")
    return 0


def run_status(*, out: Callable[[str], None] = print, directory: Union[str, Path, None] = None) -> int:
    """`pihub board-leds status`: each board LED, whether it is lit, and whether this user may switch it."""
    leds = BoardLeds(directory)
    names = leds.names()
    if not names:
        out("No Raspberry Pi board LEDs here.")
        return 0
    for name in names:
        led = leds.directory / name
        lit = led.joinpath("brightness").read_text().strip() != "0"
        writable = os.access(led / "brightness", os.W_OK) and os.access(led / "trigger", os.W_OK)
        trigger = _current_trigger(led.joinpath("trigger").read_text())
        out(f"{name}: {'on' if lit else 'off'} (trigger {trigger}); {'this user may switch it' if writable else 'root only: ' + ALLOW_COMMAND}")
    return 0
