"""`pihub.gpio.prepare_pins`: gpiozero's pin driver (`lgpio`) is found from a venv that cannot see Raspberry Pi OS's packages.

Found on a Pi 3A+ (Trixie, 2026-09-23): `lgpio` does not build under pip, and without it gpiozero's fallback driver fails on current
kernels. Runs anywhere: a folder with a pretend `lgpio` stands in for the system's packages.
"""

from __future__ import annotations

import sys

import pytest

from pihub.gpio import prepare_pins


@pytest.fixture
def no_lgpio(monkeypatch):
    """This Python has no `lgpio` of its own, and whatever the test adds to `sys.path` is taken away again."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "lgpio", raising=False)
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    yield
    sys.modules.pop("lgpio", None)


def system_packages(tmp_path, with_lgpio: bool = True) -> str:
    folder = tmp_path / "dist-packages"
    folder.mkdir()
    if with_lgpio:
        (folder / "lgpio.py").write_text("PRETEND = True\n")
    return str(folder)


def test_the_system_lgpio_is_found_from_a_venv(tmp_path, no_lgpio):
    if "lgpio" in sys.modules or _has_own_lgpio():
        pytest.skip("this Python has its own lgpio")
    folder = system_packages(tmp_path)

    assert prepare_pins(folder) is True
    assert sys.path[-1] == folder  # appended: the venv's own packages still come first
    import lgpio

    assert lgpio.PRETEND


def test_nothing_is_added_when_the_system_has_no_lgpio_either(tmp_path, no_lgpio):
    if _has_own_lgpio():
        pytest.skip("this Python has its own lgpio")
    folder = system_packages(tmp_path, with_lgpio=False)

    assert prepare_pins(folder) is False  # not an error by itself: an older Raspberry Pi OS uses RPi.GPIO
    assert prepare_pins(str(tmp_path / "missing")) is False
    assert str(tmp_path / "missing") not in sys.path


def test_a_python_with_its_own_lgpio_is_left_alone(tmp_path, no_lgpio, monkeypatch):
    own = tmp_path / "own"
    own.mkdir()
    (own / "lgpio.py").write_text("OWN = True\n")
    monkeypatch.syspath_prepend(str(own))
    folder = system_packages(tmp_path)

    assert prepare_pins(folder) is True
    assert folder not in sys.path


def _has_own_lgpio() -> bool:
    import importlib.util

    return importlib.util.find_spec("lgpio") is not None
