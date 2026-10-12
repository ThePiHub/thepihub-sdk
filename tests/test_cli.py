"""Tests for the `pihub` command line (the link-code flow and the device id)."""

from __future__ import annotations

import json
import stat

import pytest

from pihub import Device
from pihub.cli import main
from pihub.device_config import DeviceConfig, load_device_config, save_device_config
from pihub.registration_client import RegistrationError

SERVER = "https://platform.example.com"
LINK = {
    "linkCode": "M7FR-XQ4B",
    "pollSecret": "poll-secret",
    "expiresIn": 600,
    "linkUrl": f"{SERVER}/link",
}
APPROVED = {
    "status": "approved",
    "registrationToken": "TOKEN123",
    "deviceId": "birdbox-8f4c2d",
    "ownerEmail": "owner@example.com",
}


class FakePlatform:
    """A scripted server: answers each URL from a queue, records every call."""

    def __init__(self, polls=None, link=LINK, register=None) -> None:
        self.link = link
        self.polls = list(polls if polls is not None else [APPROVED])
        self.register_response = register or {"deviceId": "birdbox-8f4c2d", "deviceSecret": "s3cret"}
        self.calls: list = []

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        if url.endswith("/device-link"):
            return self.link
        if url.endswith("/device-link/poll"):
            step = self.polls.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        if url.endswith("/devices/register"):
            return self.register_response
        raise AssertionError(f"unexpected call {url}")

    def urls(self) -> list:
        return [url.removeprefix(SERVER) for url, _ in self.calls]


class Harness:
    def __init__(self, tmp_path, answers=("y",), **platform_kwargs) -> None:
        self.config_path = tmp_path / "config.json"
        self.platform = FakePlatform(**platform_kwargs)
        self.answers = list(answers)
        self.prompts: list = []
        self.output: list = []
        self.errors: list = []
        self.now = 0.0
        self.sleeps: list = []

    def input(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self) -> float:
        # Every poll costs (up to) its long-poll wait, so an endless
        # "pending" reaches expiry in a bounded number of calls.
        self.now += 20
        return self.now

    def run(self, *argv: str) -> int:
        return main(
            list(argv),
            http_post=self.platform,
            input_fn=self.input,
            out=self.output.append,
            err=self.errors.append,
            sleep=self.sleep,
            clock=self.clock,
            config_path=self.config_path,
        )

    @property
    def text(self) -> str:
        return "\n".join(self.output)


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


def test_setup_happy_path_registers_and_writes_config(harness):
    code = harness.run("setup", "--name", "birdbox", "--server", SERVER)

    assert code == 0
    assert load_device_config(harness.config_path) == DeviceConfig(
        device_id="birdbox-8f4c2d", device_secret="s3cret", server="wss://platform.example.com"
    )
    assert harness.platform.urls() == ["/device-link", "/device-link/poll", "/devices/register"]
    assert "Registered as birdbox-8f4c2d" in harness.text


def test_setup_shows_the_code_and_where_to_enter_it(harness):
    harness.run("setup", "--server", SERVER)

    assert f"Visit:   {SERVER}/link" in harness.text
    assert "Code:    M7FR-XQ4B" in harness.text


def test_setup_suggests_a_name_and_long_polls(harness):
    harness.run("setup", "--name", "birdbox", "--server", SERVER)

    (_, create_payload), (_, poll_payload), _ = harness.platform.calls
    assert create_payload == {"deviceName": "birdbox"}
    assert poll_payload == {"pollSecret": "poll-secret", "wait": 20}


def test_setup_defaults_the_name_to_the_hostname(harness, monkeypatch):
    monkeypatch.setattr("pihub.cli.socket.gethostname", lambda: "raspberrypi")

    harness.run("setup", "--server", SERVER)

    assert harness.platform.calls[0][1] == {"deviceName": "raspberrypi"}


def test_setup_registers_with_the_token_the_platform_handed_over_and_a_fingerprint(harness):
    harness.run("setup", "--server", SERVER)

    url, payload = harness.platform.calls[-1]
    assert url == f"{SERVER}/devices/register"
    assert payload["deviceId"] == "birdbox-8f4c2d"
    assert payload["registrationToken"] == "TOKEN123"
    assert "fingerprint" in payload


def test_setup_names_the_approving_account_and_asks_before_registering(harness):
    harness.run("setup", "--server", SERVER)

    assert "Approved by owner@example.com" in harness.text
    assert harness.prompts == ["Link this device to that account? [y/N] "]


def test_setup_keeps_polling_while_the_request_is_pending(tmp_path):
    harness = Harness(tmp_path, polls=[{"status": "pending"}, {"status": "pending"}, APPROVED])

    assert harness.run("setup", "--server", SERVER) == 0
    assert harness.platform.urls().count("/device-link/poll") == 3


@pytest.mark.parametrize("answer", ["n", "N", "", "no", "maybe"])
def test_setup_stops_if_the_operator_declines(tmp_path, answer):
    harness = Harness(tmp_path, answers=[answer])

    code = harness.run("setup", "--server", SERVER)

    assert code == 1
    assert "/devices/register" not in harness.platform.urls()
    assert load_device_config(harness.config_path) is None
    assert "Nothing was registered" in harness.text


def test_setup_treats_a_closed_stdin_as_a_decline(tmp_path):
    harness = Harness(tmp_path, answers=[])

    assert harness.run("setup", "--server", SERVER) == 1
    assert load_device_config(harness.config_path) is None


@pytest.mark.parametrize("answer", ["y", "Y", "yes", " YES "])
def test_setup_accepts_yes_in_any_case(tmp_path, answer):
    assert Harness(tmp_path, answers=[answer]).run("setup", "--server", SERVER) == 0


def test_setup_reports_a_code_that_expires_while_waiting(tmp_path):
    harness = Harness(tmp_path, polls=[{"status": "pending"}] * 100)

    code = harness.run("setup", "--server", SERVER)

    assert code == 1
    assert "expired" in harness.errors[0]
    assert "/devices/register" not in harness.platform.urls()


def test_setup_reports_when_the_server_says_the_code_is_gone(tmp_path):
    harness = Harness(tmp_path, polls=[RegistrationError("registration request failed with status 404", status=404)])

    assert harness.run("setup", "--server", SERVER) == 1
    assert "expired" in harness.errors[0]


def test_setup_survives_a_network_blip_while_waiting(tmp_path):
    harness = Harness(
        tmp_path,
        polls=[RegistrationError("registration request failed: timed out"), RegistrationError("status 503", status=503), APPROVED],
    )

    assert harness.run("setup", "--server", SERVER) == 0
    assert len(harness.sleeps) == 2


def test_setup_gives_up_on_an_unexpected_client_error(tmp_path):
    harness = Harness(tmp_path, polls=[RegistrationError("registration request failed with status 400", status=400)])

    assert harness.run("setup", "--server", SERVER) == 1
    assert "400" in harness.errors[0]


def test_setup_reports_a_failure_to_start(tmp_path):
    harness = Harness(tmp_path)
    def refuse(url, payload):
        raise RegistrationError("registration request failed with status 429", status=429)

    code = main(
        ["setup", "--server", SERVER], http_post=refuse, out=harness.output.append, err=harness.errors.append,
        config_path=harness.config_path,
    )

    assert code == 1
    assert "could not start linking" in harness.errors[0]


def test_setup_reports_a_registration_failure_without_writing_config(tmp_path):
    harness = Harness(tmp_path)
    original = harness.platform.__call__

    def fail_on_register(url, payload):
        if url.endswith("/devices/register"):
            raise RegistrationError("registration request failed with status 409: device quota reached", status=409)
        return original(url, payload)

    code = main(
        ["setup", "--server", SERVER], http_post=fail_on_register, input_fn=harness.input,
        out=harness.output.append, err=harness.errors.append, sleep=harness.sleep, clock=harness.clock,
        config_path=harness.config_path,
    )

    assert code == 1
    assert "quota" in harness.errors[0]
    assert load_device_config(harness.config_path) is None


@pytest.mark.parametrize("server", ["http://platform.example.com", "ws://platform.example.com"])
def test_setup_refuses_a_plaintext_server(harness, server, monkeypatch):
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)

    assert harness.run("setup", "--server", server) == 2
    assert "plaintext" in harness.errors[0]
    assert harness.platform.calls == []


def test_setup_allows_plaintext_to_loopback(harness, monkeypatch):
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)

    assert harness.run("setup", "--server", "http://localhost:8000") == 0


def test_setup_allow_insecure_flag_permits_a_non_loopback_plaintext_server(harness, monkeypatch):
    """The flag itself, not just `PIHUB_ALLOW_INSECURE` in the environment - a gap `setup`/`register`
    had until now (`service install` already had this flag)."""
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)

    assert harness.run("setup", "--server", "http://platform.example.com", "--allow-insecure") == 0


def test_setup_asks_before_replacing_an_existing_registration(tmp_path):
    harness = Harness(tmp_path, answers=["n"])
    save_device_config(DeviceConfig("old-device", "old-secret", "wss://platform.example.com"), harness.config_path)

    code = harness.run("setup", "--server", SERVER)

    assert code == 1
    assert "old-device" in harness.text
    assert harness.platform.calls == []
    assert load_device_config(harness.config_path).device_id == "old-device"


def test_setup_can_replace_an_existing_registration_when_told_to(tmp_path):
    harness = Harness(tmp_path, answers=["y", "y"])
    save_device_config(DeviceConfig("old-device", "old-secret", "wss://platform.example.com"), harness.config_path)

    assert harness.run("setup", "--server", SERVER) == 0
    assert load_device_config(harness.config_path).device_id == "birdbox-8f4c2d"


def test_the_config_is_written_owner_only(harness):
    harness.run("setup", "--server", SERVER)

    assert stat.S_IMODE(harness.config_path.stat().st_mode) == 0o600


def test_register_exchanges_a_token_directly(harness):
    code = harness.run("register", "ABC123XY", "--device-id", "birdbox01", "--server", SERVER)

    assert code == 0
    assert harness.platform.urls() == ["/devices/register"]
    assert harness.platform.calls[0][1]["registrationToken"] == "ABC123XY"
    assert harness.platform.calls[0][1]["deviceId"] == "birdbox01"
    assert load_device_config(harness.config_path).device_secret == "s3cret"


def test_register_needs_a_device_id(harness):
    with pytest.raises(SystemExit):
        harness.run("register", "ABC123XY", "--server", SERVER)


def test_register_refuses_a_plaintext_server(harness, monkeypatch):
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)

    assert harness.run("register", "ABC123XY", "--device-id", "x", "--server", "http://platform.example.com") == 2
    assert harness.platform.calls == []


def test_register_allow_insecure_flag_permits_a_non_loopback_plaintext_server(harness, monkeypatch):
    monkeypatch.delenv("PIHUB_ALLOW_INSECURE", raising=False)

    code = harness.run(
        "register", "ABC123XY", "--device-id", "x", "--server", "http://platform.example.com", "--allow-insecure"
    )

    assert code == 0


def test_register_reports_a_bad_token(tmp_path):
    def reject(url, payload):
        raise RegistrationError("registration request failed with status 404: unknown registration token", status=404)

    errors: list = []
    code = main(
        ["register", "BAD", "--device-id", "x", "--server", SERVER], http_post=reject, out=lambda _m: None,
        err=errors.append, config_path=tmp_path / "config.json",
    )

    assert code == 1
    assert "unknown registration token" in errors[0]


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        main([])


def test_the_server_defaults_to_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PIHUB_SERVER", "https://env.example.com")
    harness = Harness(tmp_path)

    harness.run("setup")

    assert harness.platform.calls[0][0] == "https://env.example.com/device-link"


def test_a_device_loads_its_own_id_from_config(tmp_path):
    config_path = tmp_path / "config.json"
    save_device_config(DeviceConfig("birdbox-8f4c2d", "s3cret", "wss://platform.example.com"), config_path)

    assert Device(config_path=config_path).id == "birdbox-8f4c2d"


def test_a_device_with_no_id_and_no_config_explains_what_to_do(tmp_path):
    with pytest.raises(ValueError, match="pihub setup"):
        Device(config_path=tmp_path / "missing.json")


def test_an_explicit_id_still_wins_over_config(tmp_path):
    config_path = tmp_path / "config.json"
    save_device_config(DeviceConfig("from-config", "s3cret", "wss://platform.example.com"), config_path)

    assert Device(id="explicit", config_path=config_path).id == "explicit"


def test_a_device_onboarded_by_setup_authenticates_with_no_id_in_source(harness):
    """`Device()` then `start()` connects with no token, secret or id in the user's code."""
    harness.run("setup", "--server", SERVER)

    device = Device(config_path=harness.config_path)
    transport = device._build_authenticated_transport()

    assert device.id == "birdbox-8f4c2d"
    assert transport._url == "wss://platform.example.com/ws/devices/birdbox-8f4c2d"
    assert transport._headers == {"Authorization": "Bearer s3cret"}
    assert json.loads(harness.config_path.read_text())["deviceId"] == "birdbox-8f4c2d"


# -- every argument explains itself (the manual's command reference is generated from this help; a bare argument shows as a dash there) ---------------------


def _every_argument(parser, path="pihub"):
    """(command path, argument name) for every argument of every command, however deeply the commands nest."""
    import argparse

    found = []
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found += _every_argument(sub, f"{path} {name}")
        else:
            found.append((path, action.option_strings[0] if action.option_strings else action.dest, action.help))
    return found


def test_every_argument_of_every_command_has_help_text():
    from pihub.cli import build_parser

    arguments = _every_argument(build_parser())
    assert len(arguments) >= 12, "walked too few arguments: update this test to match how the parser is built"
    missing = [f"{path} {name}" for path, name, help_text in arguments if not (help_text or "").strip()]
    assert not missing, f"arguments with no help text: {missing}"
