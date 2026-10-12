"""Test-environment isolation.

`pihub.device_config.default_config_path()` - used whenever a `Device`
is constructed without an explicit `config_path` (see device.py) -
resolves under `$XDG_CONFIG_HOME` when set, falling back to the real
`~/.config` otherwise. Left alone, that means any test that constructs a
transport-less `Device` (or otherwise exercises `register()` /
`start()`'s auto-reconnect path) reads and writes the *real*
`~/.config/thepihub/config.json` on whatever machine runs the suite.

This bit in practice: a manual demo session left a real config file
behind with `deviceId: "birdbox01"` - the exact id most of this suite's
`Device` fixtures use - and several `tests/test_device.py` tests started
failing because `Device(id="birdbox01")` picked it up and tried to
actually connect. This autouse fixture makes that class of failure
impossible: every test gets its own throwaway `XDG_CONFIG_HOME`, so
`default_config_path()` can never resolve to the real one, regardless of
what any individual test does or forgets to do.

Autouse and session-independent of any single test file, so it applies
uniformly to this whole `tests/` tree (transport tests, protocol tests,
examples, everything) without each of them needing to know it exists.
A test that specifically wants to exercise the *real* fallback behaviour
(no `$XDG_CONFIG_HOME` at all) still can - `monkeypatch.delenv()` inside
that test overrides this fixture's `monkeypatch.setenv()` for the
duration of that one test only, exactly like overriding any other
fixture-provided default.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg_config_home(monkeypatch, tmp_path):
    """Point `$XDG_CONFIG_HOME` at a per-test temp directory.

    Never touches the real `~/.config/thepihub` - no test in this suite
    may read or write it.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    """Tests run as if no person is at a terminal, whatever runs them (`pytest -s` in a real terminal included).

    `Device(register_if_needed=None)`, the default, registers itself on `start()` only when standard input is a terminal
    (`pihub.device._stdin_is_interactive`). Without this, a suite run by hand in a terminal would start prompting for link codes.
    A test of the terminal case sets it itself (`monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)`).
    """
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: False)
