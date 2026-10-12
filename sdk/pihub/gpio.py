"""GPIO pins through gpiozero, in a virtual environment as well as in Raspberry Pi OS's own Python.

gpiozero is pure Python, but it drives the pins through a driver. On current Raspberry Pi OS that driver is `lgpio`, a C
extension that apt installs for the system Python (`python3-lgpio`) and that does not build under pip. A venv made without
`--system-site-packages` therefore has gpiozero and no driver, and gpiozero falls back to its own sysfs driver, which fails on
current kernels (`OSError: [Errno 22] Invalid argument` exporting a pin: the old GPIO numbering). Found on a Pi 3A+ running
Trixie, 2026-09-23. So, as `pihub.video` does for picamera2, `prepare_pins()` lets the venv see the system's `lgpio`.
"""

from __future__ import annotations

import importlib
import os
import sys

SYSTEM_PACKAGES = "/usr/lib/python3/dist-packages"  # where Raspberry Pi OS's apt puts Python packages

PIN_DRIVER_HELP = (
    "no GPIO pin driver: on Raspberry Pi OS run `sudo apt install python3-lgpio`, "
    "and make sure this program's Python can see system packages"
)


class GpioUnavailable(RuntimeError):
    """gpiozero cannot drive the pins here. The message says what to do."""


def prepare_pins(system_packages: str = SYSTEM_PACKAGES) -> bool:
    """Make gpiozero's `lgpio` driver importable: from this Python if it has it, else from Raspberry Pi OS's system packages.

    Call it before making the first gpiozero device. Returns True if `lgpio` can now be imported. False is not an error by itself
    (an older Raspberry Pi OS drives pins with RPi.GPIO instead); if gpiozero then fails to open a pin, raise
    `GpioUnavailable(PIN_DRIVER_HELP)`.
    """
    if _importable("lgpio"):
        return True
    if not os.path.isdir(system_packages) or system_packages in sys.path:
        return False
    sys.path.append(system_packages)  # appended: whatever the venv has of its own still comes first
    importlib.invalidate_caches()
    return _importable("lgpio")


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True
