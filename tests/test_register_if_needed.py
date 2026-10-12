"""Tests for `Device(..., register_if_needed=...)`: the
tinkerer path - `start()` registers itself, interactively, the first time it's run, with no separate CLI step. The
default (`None`) does so exactly when a person is at a terminal; `True` forces it and `False` forbids it.

`request_link_code`/`poll_until_approved` are monkeypatched directly (imported names in
`pihub.device`'s own namespace) rather than threaded through as constructor parameters - `start()`
takes no injectable HTTP seam today (only `register()` does), and this is the same pattern
`test_device_link_service.py` already uses for a comparable case.

Most tests call `_run_register_if_needed()` directly rather than `start()` - once registration
succeeds, `start()` goes on to actually connect (a real network attempt), which is already covered
by other tests (`test_build_authenticated_transport_uses_the_persisted_secret_and_server` etc.) and
not what these are about. Only the *gating* logic (does the fallback fire at all) needs `start()`
itself, using a loopback address that fails fast rather than a real server.
"""

from __future__ import annotations

import pytest

from pihub import Device
from pihub.device_config import load_device_config
from pihub.link_code_client import LinkExpiredError
from pihub.registration_client import RegistrationError

LINK = {"linkCode": "M7FR-XQ4B", "pollSecret": "poll-secret", "expiresIn": 600, "linkUrl": "https://x/link"}
APPROVED = {
    "status": "approved",
    "registrationToken": "TOKEN123",
    "deviceId": "my-thing",
    "ownerEmail": "owner@example.com",
}


@pytest.fixture
def fake_link(monkeypatch):
    """Wires `request_link_code`/`poll_until_approved` to a scripted success, and `Device.register()`
    to a scripted secret - records every call for assertions."""
    calls: dict = {}

    def fake_request_link_code(server, name, *, device_id=None, post=None):
        calls["request_link"] = {"server": server, "name": name, "device_id": device_id}
        return LINK

    def fake_poll_until_approved(server, link, **kwargs):
        calls["poll"] = {"server": server, "link": link}
        return APPROVED

    def fake_register(self, token, *, server, http_post=None, allow_insecure=None):
        calls["register"] = {"token": token, "server": server, "id": self.id}
        from pihub.device_config import DeviceConfig, save_device_config

        config = DeviceConfig(device_id=self.id, device_secret="s3cret", server=server.replace("http", "ws"))
        save_device_config(config, self._config_path)
        return config

    monkeypatch.setattr("pihub.device.request_link_code", fake_request_link_code)
    monkeypatch.setattr("pihub.device.poll_until_approved", fake_poll_until_approved)
    monkeypatch.setattr(Device, "register", fake_register)
    return calls


# -- `_run_register_if_needed()` itself --------------------------------------------------------------------------


def test_registers_interactively_under_its_own_id(tmp_path, monkeypatch, fake_link, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    device._run_register_if_needed()

    assert fake_link["request_link"]["device_id"] == "my-thing"
    assert fake_link["register"]["token"] == "TOKEN123"
    assert fake_link["register"]["id"] == "my-thing"
    assert "not registered yet" in capsys.readouterr().out


def test_declining_the_confirmation_raises_and_registers_nothing(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    config_path = tmp_path / "config.json"
    device = Device(id="my-thing", config_path=config_path, register_if_needed=True)

    with pytest.raises(RuntimeError, match="cancelled"):
        device._run_register_if_needed()

    assert "register" not in fake_link
    assert load_device_config(config_path) is None


def test_a_closed_stdin_is_treated_as_a_decline(tmp_path, monkeypatch, fake_link):
    def raise_eof(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    with pytest.raises(EOFError):
        device._run_register_if_needed()


def test_an_expired_link_raises_runtimeerror(tmp_path, monkeypatch):
    monkeypatch.setattr("pihub.device.request_link_code", lambda *a, **k: LINK)

    def expire(*args, **kwargs):
        raise LinkExpiredError()

    monkeypatch.setattr("pihub.device.poll_until_approved", expire)
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    with pytest.raises(RuntimeError, match="expired"):
        device._run_register_if_needed()


def test_a_refused_link_request_raises_runtimeerror(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise RegistrationError("nope", status=400)

    monkeypatch.setattr("pihub.device.request_link_code", refuse)
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    with pytest.raises(RuntimeError, match="could not start linking"):
        device._run_register_if_needed()


def test_the_explicit_server_argument_is_used(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(
        id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True, server="http://localhost:8000"
    )

    device._run_register_if_needed()

    assert fake_link["request_link"]["server"] == "http://localhost:8000"
    assert fake_link["register"]["server"] == "http://localhost:8000"


def test_the_server_falls_back_to_the_environment(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    monkeypatch.setenv("PIHUB_SERVER", "https://dev.example.com")
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    device._run_register_if_needed()

    assert fake_link["request_link"]["server"] == "https://dev.example.com"


def test_the_server_falls_back_to_the_default_platform(tmp_path, monkeypatch, fake_link):
    monkeypatch.delenv("PIHUB_SERVER", raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True)

    device._run_register_if_needed()

    assert fake_link["request_link"]["server"] == "https://thepihub.com"


def test_an_insecure_server_with_no_override_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)
    device = Device(
        id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True,
        server="http://platform.example.com",
    )

    with pytest.raises(RuntimeError, match="plaintext"):
        device._run_register_if_needed()


# -- whether `start()` triggers the fallback at all (the gating logic) -------------------------------------------


def test_start_triggers_the_fallback_when_no_config_exists(tmp_path, monkeypatch, fake_link):
    """`start()` goes on to actually connect once registered - a real network attempt, against this
    deliberately-unreachable loopback address. Per the SDK's own "never gives up" reconnect design
    (`reconnect.py`), a refused first connection is logged and retried in the background rather than
    raised - `start()` itself returns normally. What matters here is that the fallback fired at all."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(
        id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True, server="http://localhost:1"
    )

    device.start()  # does not raise - see the docstring above
    device.stop()

    assert fake_link["request_link"]["device_id"] == "my-thing"


def test_start_skips_the_fallback_when_a_config_already_exists(tmp_path, fake_link):
    from pihub.device_config import DeviceConfig, save_device_config

    config_path = tmp_path / "config.json"
    save_device_config(DeviceConfig("my-thing", "existing-secret", "ws://localhost:1/"), config_path)
    device = Device(id="my-thing", config_path=config_path, register_if_needed=True)

    device.start()
    device.stop()

    assert "request_link" not in fake_link
    assert "register" not in fake_link


def test_with_no_terminal_and_the_flag_left_out_it_stays_silent(tmp_path, fake_link):
    """No terminal (a service, cron, a pipe) and the flag not set: a device with no config stays transportless,
    per the SDK's existing, established behaviour (`Device.send()`'s own docstring). Nothing can wait for a human."""
    device = Device(id="my-thing", config_path=tmp_path / "does-not-exist.json")

    device.start()  # should not raise - no network attempt at all, since no transport is ever built
    device.stop()

    assert device._transport is None
    assert "request_link" not in fake_link


# -- no id given: the platform assigns one (as `pihub setup` does) -----------------------------------------------


def test_no_id_is_allowed_when_registering_if_needed(tmp_path):
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True)

    assert device.id is None


def test_no_id_and_no_saved_config_is_an_error_with_no_terminal(tmp_path):
    with pytest.raises(ValueError, match="register_if_needed"):
        Device(config_path=tmp_path / "config.json")


def test_no_id_and_no_saved_config_is_an_error_when_told_never_to_register(tmp_path, monkeypatch):
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)
    with pytest.raises(ValueError, match="no device id"):
        Device(config_path=tmp_path / "config.json", register_if_needed=False)


def test_no_id_with_a_transport_is_an_error(tmp_path):
    """A transport given means `start()` never registers, so there would never be an id."""
    with pytest.raises(ValueError, match="no device id"):
        Device(transport=object(), config_path=tmp_path / "config.json", register_if_needed=True)  # type: ignore[arg-type]


def test_a_saved_config_supplies_the_id_even_with_the_flag(tmp_path):
    from pihub.device_config import DeviceConfig, save_device_config

    config_path = tmp_path / "config.json"
    save_device_config(DeviceConfig("saved-id", "secret", "ws://localhost:1/"), config_path)

    assert Device(config_path=config_path, register_if_needed=True).id == "saved-id"


def test_registering_with_no_id_takes_the_one_the_platform_assigned(tmp_path, monkeypatch, fake_link, capsys):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True)

    device._run_register_if_needed()

    assert fake_link["request_link"]["device_id"] is None  # nothing asked for: the platform chooses
    assert device.id == "my-thing"  # APPROVED["deviceId"]
    assert fake_link["register"]["id"] == "my-thing"
    output = capsys.readouterr().out
    assert "This device is not registered yet." in output
    assert "Registered as 'my-thing'" in output


def test_a_declined_registration_with_no_id_leaves_it_without_one(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True)

    with pytest.raises(RuntimeError, match="cancelled"):
        device._run_register_if_needed()

    assert device.id is None
    assert "register" not in fake_link


def test_start_with_no_id_registers_and_then_knows_its_id(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True, server="http://localhost:1")

    device.start()  # see test_start_triggers_the_fallback_when_no_config_exists for why this does not raise
    device.stop()

    assert device.id == "my-thing"
    assert load_device_config(tmp_path / "config.json").device_id == "my-thing"


def test_registering_with_a_token_needs_an_id(tmp_path):
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True)

    with pytest.raises(RuntimeError, match="no id yet"):
        device.register("TOKEN", server="http://localhost:1")


def test_device_information_can_be_built_before_the_id_is_known(tmp_path):
    """The "Device id" field is read when needed, not captured when built, so it is there (and right) once the device registers."""
    device = Device(config_path=tmp_path / "config.json", register_if_needed=True)

    information = device.information  # before start(): no id yet

    assert information.widget("device-id") is not None


# -- the default: automatic, on a terminal ---------------------------------------------------------------------


def test_at_a_terminal_no_flag_and_no_id_is_allowed_and_the_platform_assigns_one(tmp_path, monkeypatch):
    """The point of the default: `Device()`, nothing more, at a terminal on a machine never registered."""
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)

    device = Device(config_path=tmp_path / "config.json")

    assert device.id is None
    assert device._register_if_needed is True


def test_at_a_terminal_no_flag_start_registers_the_first_time(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(config_path=tmp_path / "config.json", server="http://localhost:1")

    device.start()  # registers, then tries (and fails fast) to connect: see the gating test above
    device.stop()

    assert fake_link["request_link"]["device_id"] is None  # no id given: the platform assigns one
    assert device.id == "my-thing"


def test_at_a_terminal_no_flag_a_saved_config_means_no_prompt(tmp_path, monkeypatch, fake_link):
    from pihub.device_config import DeviceConfig, save_device_config

    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)
    config_path = tmp_path / "config.json"
    save_device_config(DeviceConfig("my-thing", "existing-secret", "ws://localhost:1/"), config_path)
    device = Device(config_path=config_path)

    device.start()
    device.stop()

    assert device.id == "my-thing"
    assert "request_link" not in fake_link


def test_at_a_terminal_false_never_registers(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=False)

    device.start()
    device.stop()

    assert "request_link" not in fake_link


def test_with_no_terminal_true_still_registers(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    device = Device(id="my-thing", config_path=tmp_path / "config.json", register_if_needed=True, server="http://localhost:1")

    device.start()
    device.stop()

    assert fake_link["request_link"]["device_id"] == "my-thing"


def test_a_transport_given_means_no_registration_even_at_a_terminal(tmp_path, monkeypatch, fake_link):
    monkeypatch.setattr("pihub.device._stdin_is_interactive", lambda: True)

    class Quiet:
        def send(self, data): ...

    device = Device(id="my-thing", transport=Quiet(), config_path=tmp_path / "config.json")

    device.start()
    device.stop()

    assert "request_link" not in fake_link


# -- what counts as a terminal ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stdin_kind", "expected"),
    [("terminal", True), ("pipe", False), ("closed", False), ("none", False)],
)
def test_a_terminal_means_standard_input_is_one(monkeypatch, stdin_kind, expected):
    import sys

    from pihub import device as device_module

    class Terminal:
        def isatty(self):
            return True

    class Pipe:
        def isatty(self):
            return False

    class Closed:
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    monkeypatch.undo()  # the autouse fixture's fake, so the real function is under test
    monkeypatch.setattr(sys, "stdin", {"terminal": Terminal(), "pipe": Pipe(), "closed": Closed(), "none": None}[stdin_kind])

    assert device_module._stdin_is_interactive() is expected

