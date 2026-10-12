"""Tests for `pihub install`/`pihub uninstall` (the
consumer path): register (if needed) and install as a boot service, in one step, composing
`run_setup()` and `service.run_install()` unmodified.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pihub.cli import run_pihub_install, run_pihub_uninstall
from pihub import service
from pihub.device_config import DeviceConfig, load_device_config, save_device_config

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


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class Fake:
    """Records the calls a systemd runner receives, per `test_service.py`'s own convention."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, arguments):
        self.calls.append(arguments)
        for prefix, reply in self.answers.items():
            if arguments[: len(prefix)] == list(prefix):
                return reply(arguments) if callable(reply) else reply
        return completed()


class FakePlatform:
    """A scripted server for the registration half, per `test_cli.py`'s own convention."""

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
            return self.polls.pop(0)
        if url.endswith("/devices/register"):
            return self.register_response
        raise AssertionError(f"unexpected call {url}")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "apps" / "birdbox_live.py"
    path.parent.mkdir()
    path.write_text("print('hi')\n")
    return path


def install(script, config_home, answers=("y",), **kwargs):
    platform = kwargs.pop("platform", FakePlatform())
    output, errors, prompts = [], [], []

    def fake_input(prompt):
        prompts.append(prompt)
        return list(answers).pop(0) if answers else (_ for _ in ()).throw(EOFError)

    systemctl = kwargs.pop("systemctl", Fake())
    loginctl = kwargs.pop("loginctl", Fake({("show-user",): completed("yes\n")}))
    code = run_pihub_install(
        str(script), server=SERVER, config_home=str(config_home), http_post=platform, input_fn=fake_input,
        out=output.append, err=errors.append, sleep=lambda _s: None, clock=lambda: 0.0,
        python="/venv/bin/python", systemctl=systemctl, loginctl=loginctl, user="nick", **kwargs,
    )
    return SimpleNamespace(
        code=code, out="\n".join(output), err="\n".join(errors), platform=platform, systemctl=systemctl,
    )


def test_registers_then_installs_when_nothing_is_registered_yet(script, tmp_path):
    config_home = tmp_path / "config-home"

    result = install(script, config_home)

    assert result.code == 0
    config_path = config_home / "thepihub" / "config.json"
    assert load_device_config(config_path).device_id == "birdbox-8f4c2d"
    assert service.unit_path("birdbox-live").exists()


def test_skips_registration_when_already_registered(script, tmp_path):
    config_home = tmp_path / "config-home"
    config_path = config_home / "thepihub" / "config.json"
    save_device_config(DeviceConfig("existing-id", "existing-secret", "wss://x/"), config_path)

    result = install(script, config_home)

    assert result.code == 0
    assert result.platform.calls == []  # no network call at all: registration was skipped
    assert service.unit_path("birdbox-live").exists()
    assert load_device_config(config_path).device_id == "existing-id"  # untouched


def test_running_it_twice_is_idempotent(script, tmp_path):
    config_home = tmp_path / "config-home"

    first = install(script, config_home)
    second = install(script, config_home)

    assert (first.code, second.code) == (0, 0)
    assert len(second.platform.calls) == 0  # second run: already registered, straight to service


def test_a_declined_registration_does_not_install_the_service(script, tmp_path):
    config_home = tmp_path / "config-home"

    result = install(script, config_home, answers=("n",))

    assert result.code == 1
    assert not service.unit_path("birdbox-live").exists()


def test_device_name_and_service_name_are_independent(script, tmp_path):
    """The naming conflict the design doc calls out: `--device-name` (the link-code label) must not
    collide with `--name` (the systemd service name)."""
    config_home = tmp_path / "config-home"

    result = install(script, config_home, device_name="Bird box", name="custom-service-name")

    assert result.platform.calls[0][1]["deviceName"] == "Bird box"
    assert service.unit_path("custom-service-name").exists()
    assert not service.unit_path("birdbox-live").exists()


def test_config_home_is_used_for_both_registration_and_the_service(script, tmp_path):
    config_home = tmp_path / "config-home"

    install(script, config_home)

    unit_text = service.unit_path("birdbox-live").read_text()
    assert f"XDG_CONFIG_HOME={config_home.resolve()}" in unit_text


def test_allow_insecure_is_forwarded_to_the_service(script, tmp_path):
    config_home = tmp_path / "config-home"

    install(script, config_home, allow_insecure=True)

    unit_text = service.unit_path("birdbox-live").read_text()
    assert "PIHUB_ALLOW_INSECURE=1" in unit_text


def test_program_arguments_are_forwarded_to_the_service(script, tmp_path):
    config_home = tmp_path / "config-home"

    install(script, config_home, args=["--size", "1280x720"])

    unit_text = service.unit_path("birdbox-live").read_text()
    assert "--size 1280x720" in unit_text


def test_pihub_uninstall_is_a_passthrough_to_service_uninstall(script, tmp_path):
    config_home = tmp_path / "config-home"
    install(script, config_home)
    fake, output = Fake(), []

    code = run_pihub_uninstall("birdbox-live", systemctl=fake, out=output.append)

    assert code == 0
    assert not service.unit_path("birdbox-live").exists()
    assert "no longer start at boot" in output[0]


# -- the real argparse wiring (`main()`), not bypassing it like the tests above -----------------------------------


def test_main_parses_install_and_forwards_every_flag(monkeypatch):
    from pihub import cli

    captured = {}

    def fake_run_pihub_install(script, **kwargs):
        captured["script"] = script
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_pihub_install", fake_run_pihub_install)

    code = cli.main(
        [
            "install", "apps/birdbox_live.py",
            "--device-name", "Bird box",
            "--name", "birdbox-live",
            "--config-home", "/tmp/config-home",
            "--server", SERVER,
            "--allow-insecure",
            "--", "--size", "1280x720",
        ]
    )

    assert code == 0
    assert captured["script"] == "apps/birdbox_live.py"
    assert captured["device_name"] == "Bird box"
    assert captured["name"] == "birdbox-live"
    assert captured["config_home"] == "/tmp/config-home"
    assert captured["server"] == SERVER
    assert captured["allow_insecure"] is True
    assert captured["args"] == ["--size", "1280x720"]


def test_main_install_defaults_need_only_the_script(monkeypatch):
    from pihub import cli

    captured = {}
    monkeypatch.setattr(cli, "run_pihub_install", lambda script, **kwargs: captured.update(kwargs) or 0)

    cli.main(["install", "apps/birdbox_live.py"])

    assert captured["device_name"] is None
    assert captured["name"] is None
    assert captured["config_home"] is None
    assert captured["allow_insecure"] is False


def test_main_parses_uninstall(monkeypatch):
    from pihub import cli

    captured = {}
    monkeypatch.setattr(cli, "run_pihub_uninstall", lambda name, **kwargs: captured.update(name=name) or 0)

    code = cli.main(["uninstall", "birdbox-live"])

    assert code == 0
    assert captured["name"] == "birdbox-live"
