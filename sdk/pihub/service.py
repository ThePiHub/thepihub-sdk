"""`pihub service`: run a program at boot, and keep it running.

Opt-in and explicit. Nothing here happens unless someone runs the command:

    pihub service install apps/birdbox_live.py [--name birdbox] [--config-home DIR]
    pihub service status [birdbox]
    pihub service uninstall birdbox

It writes a systemd *user* service (no root needed) that runs the script with
the interpreter `pihub` itself is running under - the virtualenv - and
restarts it if it exits. It relies on the SDK reconnecting by itself, so it
does not need to be ordered after the network: a program that starts before
wifi is up simply connects when it can.

One thing does need root, once: a user service stops when its owner's last
login session ends, so on a headless Pi it only survives (and only starts at
boot) if lingering is enabled. `install` checks and prints the exact command
rather than running `sudo` for you.

The unit lives in `~/.config/systemd/user` *always* - not under
`$XDG_CONFIG_HOME`. That variable is how a second identity is kept apart from
the default one, and systemd's user manager does not look in it.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

UNIT_PREFIX = "pihub-"
RESTART_SECONDS = 10

Runner = Callable[[List[str]], "subprocess.CompletedProcess[str]"]


def unit_directory() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def service_name(script: Path, name: Optional[str]) -> str:
    """A safe, readable service name: the given one, else the script's name."""
    raw = name if name else script.stem
    cleaned = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return cleaned or "program"


def unit_path(name: str) -> Path:
    return unit_directory() / f"{UNIT_PREFIX}{name}.service"


_SAFE = re.compile(r"^[A-Za-z0-9_@+=:,./-]+$")


def systemd_quote(value: str) -> str:
    """Quote one word for a unit file's `ExecStart` / `Environment`.

    systemd splits on whitespace and expands `%` and `$` itself, so all
    three have to be handled or a path with a space (or a `%`) silently
    becomes a different command.
    """
    escaped = value.replace("%", "%%").replace("$", "$$")
    if _SAFE.match(escaped):
        return escaped
    return '"' + escaped.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_unit(
    name: str,
    script: Path,
    *,
    python: str,
    args: List[str],
    workdir: Path,
    config_home: Optional[Path] = None,
    allow_insecure: bool = False,
) -> str:
    # PIHUB_SERVICE makes the program wait for the identity when another program holds it, instead of exiting
    # (device_lock.py): a boot service should take over as soon as an experiment in a terminal ends.
    environment = [f"Environment={systemd_quote('PIHUB_SERVICE=' + name)}"]
    if config_home is not None:
        environment.append(f"Environment={systemd_quote('XDG_CONFIG_HOME=' + str(config_home))}")
    if allow_insecure:
        environment.append("Environment=PIHUB_ALLOW_INSECURE=1")
    command = " ".join(systemd_quote(part) for part in [python, "-u", str(script), *args])
    lines = [
        "[Unit]",
        f"Description=ThePiHub program: {name}",
        "",
        "[Service]",
        "Type=simple",
        # Unlike ExecStart and Environment, this takes the rest of the line as
        # the path: quotes would become part of it (systemd rejects the unit),
        # and only `%` needs escaping.
        f"WorkingDirectory={str(workdir).replace('%', '%%')}",
        *environment,
        f"ExecStart={command}",
        "Restart=always",
        f"RestartSec={RESTART_SECONDS}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _default_systemctl(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["systemctl", "--user", *arguments], capture_output=True, text=True)


def _lingering(user: str, runner: Callable[[List[str]], "subprocess.CompletedProcess[str]"]) -> bool:
    result = runner(["show-user", user, "-p", "Linger", "--value"])
    return result.returncode == 0 and result.stdout.strip() == "yes"


def _default_loginctl(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["loginctl", *arguments], capture_output=True, text=True)


def _have_systemd(systemctl: Optional[Runner]) -> bool:
    return systemctl is not None or shutil.which("systemctl") is not None


def run_install(
    script_argument: str,
    *,
    name: Optional[str] = None,
    args: Optional[List[str]] = None,
    config_home: Optional[str] = None,
    allow_insecure: bool = False,
    python: Optional[str] = None,
    systemctl: Optional[Runner] = None,
    loginctl: Optional[Runner] = None,
    user: Optional[str] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> int:
    script = Path(script_argument).expanduser().resolve()
    if not script.is_file():
        err(f"error: {script_argument} is not a file")
        return 1
    if not _have_systemd(systemctl):
        err("error: systemd was not found (systemctl is missing). `pihub service` needs a Linux machine that uses systemd.")
        return 1

    run = systemctl or _default_systemctl
    chosen = service_name(script, name)
    unit = build_unit(
        chosen,
        script,
        python=python or sys.executable,
        args=args or [],
        workdir=script.parent,
        config_home=Path(config_home).expanduser().resolve() if config_home else None,
        allow_insecure=allow_insecure,
    )
    path = unit_path(chosen)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    unit_name = path.name

    for step in (["daemon-reload"], ["enable", unit_name], ["restart", unit_name]):
        result = run(step)
        if result.returncode != 0:
            err(f"error: `systemctl --user {' '.join(step)}` failed: {(result.stderr or result.stdout).strip()}")
            return 1

    out(f"Installed {unit_name} and started it.")
    out(f"  runs:    {python or sys.executable} -u {script}")
    if config_home:
        out(f"  config:  {config_home}")
    out(f"  logs:    pihub service status {chosen}   (or: journalctl --user -u {unit_name} -f)")
    out(f"  status:  pihub service status {chosen}")
    out(f"  remove:  pihub service uninstall {chosen}")

    account = user or getpass.getuser()
    if not _lingering(account, loginctl or _default_loginctl):
        out("")
        out("ONE MORE STEP for it to start at boot and survive logging out. Run this once, on this machine:")
        out(f"    sudo loginctl enable-linger {account}")
        out("(Until then it runs only while you are logged in.)")
    return 0


def run_uninstall(
    name: str,
    *,
    systemctl: Optional[Runner] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> int:
    path = unit_path(service_name(Path(name), name))
    if not path.exists():
        err(f"error: no service named {name!r} is installed (looked for {path})")
        return 1
    if _have_systemd(systemctl):
        run = systemctl or _default_systemctl
        run(["disable", "--now", path.name])  # best effort: it may already be stopped
        path.unlink()
        run(["daemon-reload"])
    else:
        path.unlink()
    out(f"Removed {path.name}. It will no longer start at boot.")
    return 0


def run_status(
    name: Optional[str],
    *,
    systemctl: Optional[Runner] = None,
    journalctl: Optional[Runner] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
) -> int:
    directory = unit_directory()
    if name is None:
        installed = sorted(p.name for p in directory.glob(f"{UNIT_PREFIX}*.service")) if directory.exists() else []
        if not installed:
            out("No pihub services are installed.")
            return 0
        for unit in installed:
            out(unit[len(UNIT_PREFIX): -len(".service")])
        return 0

    path = unit_path(service_name(Path(name), name))
    if not path.exists():
        err(f"error: no service named {name!r} is installed")
        return 1
    run = systemctl or _default_systemctl
    enabled = run(["is-enabled", path.name]).stdout.strip() or "unknown"
    active = run(["is-active", path.name]).stdout.strip() or "unknown"
    out(f"{path.name}: {active} (starts at boot: {'yes' if enabled == 'enabled' else enabled})")

    read = journalctl or _default_journalctl
    logs = read(["--user", "-u", path.name, "-n", "10", "--no-pager"])
    if not _has_log_lines(logs):
        # Many systems (a Raspberry Pi's default) keep the journal in memory
        # only, so there are no per-user journal files and the query above
        # finds nothing. The same lines are in the system journal, tagged
        # with the user unit's name.
        logs = read(["-n", "10", "--no-pager", f"_SYSTEMD_USER_UNIT={path.name}"])
    if _has_log_lines(logs):
        out("")
        out("Recent log lines:")
        out(logs.stdout.rstrip())
    return 0


def _has_log_lines(result: "subprocess.CompletedProcess[str]") -> bool:
    text = result.stdout.strip()
    return result.returncode == 0 and bool(text) and "No journal files" not in text and not text.startswith("-- No entries")


def _default_journalctl(arguments: List[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(["journalctl", *arguments], capture_output=True, text=True)
