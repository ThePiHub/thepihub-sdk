"""Tests for `pihub service` - the opt-in systemd user service. systemctl,
loginctl and journalctl are faked, and HOME is a temp folder, so nothing here
touches the real machine."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from pihub import service
from pihub.cli import main


def completed(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


class Fake:
    """Records the calls a runner receives and answers from a table."""

    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, arguments):
        self.calls.append(arguments)
        for prefix, reply in self.answers.items():
            if arguments[: len(prefix)] == list(prefix):
                return reply(arguments) if callable(reply) else reply
        return completed()


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


def install(script, **kwargs):
    output, errors = [], []
    systemctl = kwargs.pop("systemctl", Fake())
    loginctl = kwargs.pop("loginctl", Fake({("show-user",): completed("yes\n")}))
    code = service.run_install(
        str(script), python="/venv/bin/python", systemctl=systemctl, loginctl=loginctl, user="nick",
        out=output.append, err=errors.append, **kwargs,
    )
    return SimpleNamespace(code=code, out="\n".join(output), err="\n".join(errors), systemctl=systemctl)


# ------------------------------------------------------------------ the unit file

def test_the_unit_runs_the_script_with_the_given_interpreter_and_restarts_it(script, home):
    result = install(script)

    text = service.unit_path("birdbox-live").read_text()
    assert result.code == 0
    assert f"ExecStart=/venv/bin/python -u {script}" in text
    assert "Restart=always" in text and "RestartSec=10" in text
    assert "WantedBy=default.target" in text
    assert f"WorkingDirectory={script.parent}" in text


def test_the_unit_lives_in_the_users_systemd_folder(script, home):
    install(script)

    assert service.unit_path("birdbox-live") == home / ".config" / "systemd" / "user" / "pihub-birdbox-live.service"
    assert service.unit_path("birdbox-live").exists()


def test_the_unit_folder_ignores_xdg_config_home(script, home, monkeypatch, tmp_path):
    """XDG_CONFIG_HOME picks a separate saved identity; systemd's user manager
    does not look there, so the unit must not follow it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))

    install(script)

    assert service.unit_path("birdbox-live").parent == home / ".config" / "systemd" / "user"
    assert not (tmp_path / "elsewhere").exists()


def test_a_separate_identity_and_insecure_mode_are_only_set_when_asked(script, tmp_path):
    plain = install(script)
    assert "XDG_CONFIG_HOME" not in service.unit_path("birdbox-live").read_text()
    assert "ALLOW_INSECURE" not in service.unit_path("birdbox-live").read_text()

    install(script, config_home=str(tmp_path / "cfg-test"), allow_insecure=True)
    text = service.unit_path("birdbox-live").read_text()
    assert f"Environment=XDG_CONFIG_HOME={tmp_path / 'cfg-test'}" in text
    assert "Environment=PIHUB_ALLOW_INSECURE=1" in text


def test_program_arguments_are_passed_through(script):
    install(script, args=["--server", "http://x:8000"])

    assert f"-u {script} --server http://x:8000" in service.unit_path("birdbox-live").read_text()


@pytest.mark.parametrize("value, expected", [
    ("/plain/path.py", "/plain/path.py"),
    ("/has space/x.py", '"/has space/x.py"'),
    ("50%", '"50%%"'),
    ("$HOME/x", '"$$HOME/x"'),
    ('say "hi"', '"say \\"hi\\""'),
])
def test_words_are_quoted_the_way_systemd_reads_them(value, expected):
    assert service.systemd_quote(value) == expected


def test_a_path_with_a_space_survives_into_the_unit(tmp_path):
    spaced = tmp_path / "my apps" / "bird box.py"
    spaced.parent.mkdir()
    spaced.write_text("x = 1\n")

    install(spaced)

    text = service.unit_path("bird-box").read_text()
    assert f'ExecStart=/venv/bin/python -u "{spaced}"' in text
    # systemd rejects a quoted WorkingDirectory (found by running a real install)
    assert f"WorkingDirectory={spaced.parent}\n" in text
    assert 'WorkingDirectory="' not in text


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("given, expected", [
    (None, "birdbox-live"), ("My Pi_4!", "my-pi-4"), ("---", "program"), ("Greenhouse", "greenhouse"),
])
def test_service_names_are_made_safe(script, given, expected):
    assert service.service_name(script, given) == expected


# ------------------------------------------------------------------ what it asks systemd to do

def test_it_reloads_enables_and_restarts_in_that_order(script):
    result = install(script)

    assert result.systemctl.calls == [
        ["daemon-reload"], ["enable", "pihub-birdbox-live.service"], ["restart", "pihub-birdbox-live.service"],
    ]


def test_installing_again_replaces_the_unit_and_restarts(script):
    install(script)
    install(script, args=["--changed"])

    assert "--changed" in service.unit_path("birdbox-live").read_text()


def test_a_systemd_failure_is_reported_and_stops(script):
    failing = Fake({("enable",): completed(returncode=1, stderr="Failed to connect to bus")})

    result = install(script, systemctl=failing)

    assert result.code == 1 and "Failed to connect to bus" in result.err
    assert ["restart", "pihub-birdbox-live.service"] not in failing.calls


# ------------------------------------------------------------------ refusing sensibly

def test_a_missing_script_is_refused_and_nothing_is_written(tmp_path, home):
    result = install(tmp_path / "nope.py")

    assert result.code == 1 and "is not a file" in result.err
    assert not (home / ".config").exists()


def test_a_machine_without_systemd_is_told_so(script, monkeypatch, home):
    monkeypatch.setattr(service.shutil, "which", lambda name: None)
    output, errors = [], []

    code = service.run_install(str(script), out=output.append, err=errors.append)

    assert code == 1 and "systemd was not found" in errors[0]
    assert not (home / ".config").exists()


# ------------------------------------------------------------------ linger

def test_when_lingering_is_off_it_prints_the_exact_command_and_does_not_run_sudo(script):
    result = install(script, loginctl=Fake({("show-user",): completed("no\n")}))

    assert "sudo loginctl enable-linger nick" in result.out
    assert "ONE MORE STEP" in result.out


def test_when_lingering_is_on_there_is_nothing_more_to_do(script):
    result = install(script, loginctl=Fake({("show-user",): completed("yes\n")}))

    assert "enable-linger" not in result.out


def test_an_unanswerable_linger_check_is_treated_as_off(script):
    result = install(script, loginctl=Fake({("show-user",): completed(returncode=1)}))

    assert "enable-linger" in result.out


# ------------------------------------------------------------------ uninstall and status

def test_uninstall_stops_removes_and_reloads(script):
    install(script)
    fake, output = Fake(), []

    code = service.run_uninstall("birdbox-live", systemctl=fake, out=output.append)

    assert code == 0
    assert not service.unit_path("birdbox-live").exists()
    assert fake.calls == [["disable", "--now", "pihub-birdbox-live.service"], ["daemon-reload"]]
    assert "no longer start at boot" in output[0]


def test_uninstalling_something_that_is_not_installed_says_so(home):
    errors = []

    code = service.run_uninstall("ghost", systemctl=Fake(), err=errors.append)

    assert code == 1 and "no service named 'ghost'" in errors[0]


def test_status_shows_state_and_recent_logs(script):
    install(script)
    fake = Fake({("is-enabled",): completed("enabled\n"), ("is-active",): completed("active\n")})
    journal = Fake({("--user",): completed("Sep 19 device online\n")})
    output = []

    service.run_status("birdbox-live", systemctl=fake, journalctl=journal, out=output.append)

    text = "\n".join(output)
    assert "pihub-birdbox-live.service: active (starts at boot: yes)" in text
    assert "device online" in text


def test_status_falls_back_to_the_system_journal_when_there_are_no_user_journal_files(script):
    """A Raspberry Pi keeps the journal in memory only, so `journalctl --user`
    reports 'No journal files were found' - found on a real Pi. The same lines
    are in the system journal, tagged with the user unit."""
    install(script)
    fake = Fake({("is-enabled",): completed("enabled\n"), ("is-active",): completed("active\n")})
    journal = Fake({
        ("--user",): completed("No journal files were found.\n-- No entries --\n"),
        ("-n",): completed("Sep 19 python3[2697]: birdbox-live is online.\n"),
    })
    output = []

    service.run_status("birdbox-live", systemctl=fake, journalctl=journal, out=output.append)

    assert "birdbox-live is online" in "\n".join(output)
    assert ["-n", "10", "--no-pager", "_SYSTEMD_USER_UNIT=pihub-birdbox-live.service"] in journal.calls


def test_status_does_not_show_an_empty_log_section(script):
    install(script)
    fake = Fake({("is-enabled",): completed("enabled\n"), ("is-active",): completed("active\n")})
    journal = Fake({("--user",): completed("-- No entries --\n"), ("-n",): completed("")})
    output = []

    service.run_status("birdbox-live", systemctl=fake, journalctl=journal, out=output.append)

    assert "Recent log lines" not in "\n".join(output)


def test_status_without_a_name_lists_what_is_installed(script):
    install(script)
    install(script, name="second")
    output = []

    service.run_status(None, out=output.append)

    assert output == ["birdbox-live", "second"]


def test_status_with_nothing_installed_says_so(home):
    output = []
    service.run_status(None, out=output.append)
    assert output == ["No pihub services are installed."]


# ------------------------------------------------------------------ the command line

def test_the_cli_installs_a_service_with_program_arguments_after_the_dashes(script):
    systemctl = Fake()
    code = main(
        ["service", "install", str(script), "--name", "bird", "--allow-insecure", "--", "--server", "http://x:8000"],
        systemctl=systemctl, loginctl=Fake({("show-user",): completed("yes\n")}), out=lambda _m: None,
        err=lambda _m: None, python="/venv/bin/python", user="nick",
    )

    text = service.unit_path("bird").read_text()
    assert code == 0
    assert "--server http://x:8000" in text and "PIHUB_ALLOW_INSECURE=1" in text


def test_nothing_is_installed_by_any_other_command(script, home):
    """The opt-in guarantee: setup and register never touch systemd."""
    main(["register", "TOKEN", "--device-id", "d", "--server", "https://x.example"],
         http_post=lambda url, payload: {"deviceId": "d", "deviceSecret": "s"},
         out=lambda _m: None, err=lambda _m: None, config_path=home / "config.json")

    assert not (home / ".config" / "systemd").exists()


def test_the_unit_names_its_service_so_the_program_waits_for_the_identity_instead_of_exiting(script, home):
    from pihub.service import build_unit

    unit = build_unit("birdbox", script, python="/usr/bin/python3", args=[], workdir=script.parent)
    assert "Environment=PIHUB_SERVICE=birdbox" in unit
