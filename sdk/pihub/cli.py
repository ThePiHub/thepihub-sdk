"""The `pihub` command line.

    pihub setup [--name birdbox] [--server https://...]
    pihub register TOKEN --device-id ID [--server https://...]
    pihub service install|status|uninstall ...        (see pihub.service)
    pihub camera setup [--noir | --normal] | show      (see pihub.camera_setup)

`setup` is the onboarding path for a machine with no browser - a Pi over
SSH. It asks the platform for a short link code, shows it, and waits while
the owner enters it on the platform's web page; the platform then issues
the device its id and hands over a registration token, which this command
exchanges exactly as `Device.register()` always has. Nothing about
registration tokens, device secrets or ownership changes: this only
replaces copy-pasting a token between two machines.

`register` is that older exchange, directly, for people who already have a
token and an id.
"""

from __future__ import annotations

import argparse
import functools
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

from pihub.device import Device
from pihub.device_config import default_config_path, load_device_config
from pihub.link_code_client import (
    POLL_HTTP_TIMEOUT_SECONDS,
    LinkExpiredError,
    poll_until_approved,
    request_link as request_link_code,
)
from pihub.registration_client import DEFAULT_SERVER, SERVER_ENV, HttpPost, RegistrationError, default_http_post
from pihub.security import InsecureTransportError, require_secure_url


def _yes(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes"}


def run_setup(
    server: str,
    name: Optional[str],
    *,
    http_post: Optional[HttpPost] = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    config_path: Optional[Path] = None,
    allow_insecure: bool = False,
) -> int:
    """Link this machine to an account. Returns a process exit code.

    `allow_insecure` permits a plain `http://` `server` to anything but
    loopback - mirrors `pihub service install`'s own flag of the same
    name, closing a gap this didn't have one for before: without it,
    plain HTTP only worked here if `server` happened to be loopback or the
    calling shell's own environment already had `PIHUB_ALLOW_INSECURE` set.
    """
    server = server.rstrip("/")
    try:
        require_secure_url(server, allow_insecure=allow_insecure)
    except InsecureTransportError as error:
        err(f"error: {error}")
        return 2

    if http_post is None:
        post: HttpPost = default_http_post
        poll_post: HttpPost = functools.partial(default_http_post, timeout=POLL_HTTP_TIMEOUT_SECONDS)
    else:
        post = poll_post = http_post

    existing = load_device_config(config_path)
    if existing is not None:
        out(f"This machine is already registered as {existing.device_id!r}.")
        try:
            confirmed = _yes(input_fn("Link it as a new device anyway? [y/N] "))
        except EOFError:
            confirmed = False
        if not confirmed:
            out("Cancelled. Nothing was changed.")
            return 1

    try:
        link = request_link_code(server, name or socket.gethostname(), post=post)
    except RegistrationError as error:
        err(f"error: could not start linking: {error}")
        return 1

    out("Link your device")
    out("")
    out(f"Visit:   {link['linkUrl']}")
    out(f"Code:    {link['linkCode']}")
    out("")
    out("Waiting for approval...")

    try:
        approved = poll_until_approved(server, link, poll_post=poll_post, sleep=sleep, clock=clock)
    except LinkExpiredError:
        err("error: the link code expired before it was approved. Run `pihub setup` again.")
        return 1
    except RegistrationError as error:
        err(f"error: {error}")
        return 1

    owner = approved.get("ownerEmail") or "an account"
    out("")
    out(f"Approved by {owner}")
    # Approving a code assigns *permanent* ownership, so a wrong link has
    # to be visible at the machine being linked, not only in a browser.
    try:
        confirmed = _yes(input_fn("Link this device to that account? [y/N] "))
    except EOFError:
        confirmed = False
    if not confirmed:
        out("Cancelled. Nothing was registered.")
        return 1

    return _register(
        server, approved["deviceId"], approved["registrationToken"], post, out, err, config_path, allow_insecure
    )


def run_register(
    server: str,
    token: str,
    device_id: str,
    *,
    http_post: Optional[HttpPost] = None,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    config_path: Optional[Path] = None,
    allow_insecure: bool = False,
) -> int:
    """Exchange an existing registration token directly, with no link code.

    `allow_insecure`: see `run_setup()`'s own docstring.
    """
    server = server.rstrip("/")
    try:
        require_secure_url(server, allow_insecure=allow_insecure)
    except InsecureTransportError as error:
        err(f"error: {error}")
        return 2
    return _register(server, device_id, token, http_post or default_http_post, out, err, config_path, allow_insecure)


def _register(
    server: str,
    device_id: str,
    token: str,
    http_post: HttpPost,
    out: Callable[[str], None],
    err: Callable[[str], None],
    config_path: Optional[Path],
    allow_insecure: bool = False,
) -> int:
    try:
        config = Device(id=device_id, config_path=config_path).register(
            token, server=server, http_post=http_post, allow_insecure=allow_insecure
        )
    except RegistrationError as error:
        err(f"error: {error}")
        return 1

    out("")
    out(f"Registered as {config.device_id}")
    out(f"Config written to {config_path if config_path is not None else default_config_path()}")
    return 0


def run_pihub_install(
    script: str,
    *,
    server: str,
    device_name: Optional[str] = None,
    name: Optional[str] = None,
    config_home: Optional[str] = None,
    allow_insecure: bool = False,
    args: Optional[List[str]] = None,
    http_post: Optional[HttpPost] = None,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    config_path: Optional[Path] = None,
    python: Optional[str] = None,
    systemctl: Optional[Any] = None,
    loginctl: Optional[Any] = None,
    user: Optional[str] = None,
) -> int:
    """Register (if needed) and install as a boot service, in one step
    (the consumer path).

    Checked *before* calling `run_setup()`, not left to its own internal
    check: re-running `pihub install` on an already-registered machine
    must go straight to (re)installing the service, with no "link as a
    new device anyway?" prompt - that prompt is for someone running
    `pihub setup` directly a second time, not for this composed command.

    `config_path` (like `main()`'s other injected test seams) overrides
    the path derived from `--config-home` when given, and
    `python`/`systemctl`/`loginctl`/`user` are forwarded to
    `service.run_install()` unchanged - both exist so tests can fake the
    HTTP layer and systemd the same way `test_service.py` already does for
    `service install` directly; a real invocation never passes them.
    """
    if config_path is None:
        config_path = Path(config_home).expanduser().resolve() / "thepihub" / "config.json" if config_home else None

    if load_device_config(config_path) is None:
        code = run_setup(
            server, device_name, http_post=http_post, input_fn=input_fn, out=out, err=err,
            sleep=sleep, clock=clock, config_path=config_path, allow_insecure=allow_insecure,
        )
        if code != 0:
            return code

    from pihub import service

    return service.run_install(
        script, name=name, args=args, config_home=config_home, allow_insecure=allow_insecure, out=out, err=err,
        python=python, systemctl=systemctl, loginctl=loginctl, user=user,
    )


def run_pihub_uninstall(name: str, **injected: Any) -> int:
    """Stop and remove a service (the consumer path). A bare
    passthrough to `service.run_uninstall()` under a friendlier top-level name - so a consumer never
    has to learn the word "service" at all."""
    from pihub import service

    return service.run_uninstall(name, **injected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pihub", description="ThePiHub device tools")
    commands = parser.add_subparsers(dest="command", required=True)
    default_server = os.environ.get(SERVER_ENV, DEFAULT_SERVER)

    setup = commands.add_parser("setup", help="link this machine to your account with a code")
    setup.add_argument("--name", help="a name for this device (default: this machine's hostname)")
    setup.add_argument("--server", default=default_server, help=f"platform URL (default: ${SERVER_ENV} or {DEFAULT_SERVER})")
    setup.add_argument("--allow-insecure", action="store_true", help="set PIHUB_ALLOW_INSECURE=1 (plain http/ws to a dev server)")

    register = commands.add_parser("register", help="register with a registration token you already have")
    register.add_argument("token", help="a registration token from the web app")
    register.add_argument("--device-id", required=True, help="the id to register this device under")
    register.add_argument("--server", default=default_server, help=f"platform URL (default: ${SERVER_ENV} or {DEFAULT_SERVER})")
    register.add_argument("--allow-insecure", action="store_true", help="set PIHUB_ALLOW_INSECURE=1 (plain http/ws to a dev server)")

    camera = commands.add_parser("camera", help="tell this machine what kind of camera it has (a NoIR camera needs this)")
    camera_actions = camera.add_subparsers(dest="action", required=True)
    camera_setup = camera_actions.add_parser("setup", help="look at the camera's picture, ask if it is a NoIR camera, save the answer")
    kind = camera_setup.add_mutually_exclusive_group()
    kind.add_argument("--noir", action="store_true", help="it is a NoIR camera (no question)")
    kind.add_argument("--normal", action="store_true", help="it is a normal camera (no question)")
    camera_actions.add_parser("show", help="show the saved camera setting")

    board_leds = commands.add_parser("board-leds", help="the Pi's own lights: let programs switch them off (a camera trap must not glow)")
    board_actions = board_leds.add_subparsers(dest="action", required=True)
    board_actions.add_parser("allow", help="ONCE, with sudo: let the gpio group switch the board LEDs (a udev rule)")
    board_actions.add_parser("status", help="show each board LED and whether this user may switch it")

    pihub_install = commands.add_parser(
        "install",
        help="register (if needed) and install as a boot service, in one step (its own arguments go after a --)",
    )
    pihub_install.add_argument("script", help="the Python program to run")
    pihub_install.add_argument("--device-name", help="a name for this device (default: this machine's hostname)")
    pihub_install.add_argument("--name", help="service name (default: the script's name)")
    pihub_install.add_argument("--config-home", help="run with this XDG_CONFIG_HOME, to use a separate saved identity")
    pihub_install.add_argument("--server", default=default_server, help=f"platform URL (default: ${SERVER_ENV} or {DEFAULT_SERVER})")
    pihub_install.add_argument("--allow-insecure", action="store_true", help="set PIHUB_ALLOW_INSECURE=1 (plain http/ws to a dev server)")

    pihub_uninstall = commands.add_parser("uninstall", help="stop and remove a service")
    pihub_uninstall.add_argument("name", help="the name of the service to remove (`pihub service status` lists them)")

    service = commands.add_parser("service", help="run a program at boot (opt-in, systemd)")
    actions = service.add_subparsers(dest="action", required=True)
    install = actions.add_parser(
        "install", help="install and start a service for a program (its own arguments go after a --)"
    )
    install.add_argument("script", help="the Python program to run")
    install.add_argument("--name", help="service name (default: the script's name)")
    install.add_argument("--config-home", help="run with this XDG_CONFIG_HOME, to use a separate saved identity")
    install.add_argument("--allow-insecure", action="store_true", help="set PIHUB_ALLOW_INSECURE=1 (plain http/ws to a dev server)")
    status = actions.add_parser("status", help="show a service's state and recent log lines (or list all)")
    status.add_argument("name", nargs="?", help="a service name; leave it out to list all of them")
    uninstall = actions.add_parser("uninstall", help="stop and remove a service")
    uninstall.add_argument("name", help="the name of the service to remove (`pihub service status` lists them)")
    return parser


def main(argv: Optional[List[str]] = None, **injected: Any) -> int:
    """Entry point. `injected` is for tests: `http_post`, `input_fn`, `out`,
    `err`, `sleep`, `clock` and `config_path` replace the real thing."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    # Everything after a bare `--` belongs to the program being installed, not
    # to pihub. argparse cannot express that next to interleaved options.
    program_args: List[str] = []
    if "--" in raw:
        split = raw.index("--")
        raw, program_args = raw[:split], raw[split + 1:]
    args = build_parser().parse_args(raw)
    if args.command == "install":
        return run_pihub_install(
            args.script, server=args.server, device_name=args.device_name, name=args.name,
            config_home=args.config_home, allow_insecure=args.allow_insecure, args=program_args, **injected,
        )
    if args.command == "uninstall":
        return run_pihub_uninstall(args.name, **injected)
    if args.command == "service":
        from pihub import service

        if args.action == "install":
            return service.run_install(
                args.script, name=args.name, args=program_args, config_home=args.config_home,
                allow_insecure=args.allow_insecure, **injected,
            )
        if args.action == "status":
            return service.run_status(args.name, **injected)
        return service.run_uninstall(args.name, **injected)
    if args.command == "camera":
        from pihub import camera_setup

        if args.action == "show":
            return camera_setup.run_camera_show(**{k: v for k, v in injected.items() if k in {"out", "config_path"}})
        answer = True if args.noir else False if args.normal else None
        return camera_setup.run_camera_setup(answer, **injected)
    if args.command == "board-leds":
        from pihub import board_leds

        if args.action == "allow":
            return board_leds.run_allow(**{k: v for k, v in injected.items() if k in {"out", "rule_path", "run", "is_root"}})
        return board_leds.run_status(**{k: v for k, v in injected.items() if k in {"out", "directory"}})
    if args.command == "setup":
        return run_setup(args.server, args.name, allow_insecure=args.allow_insecure, **injected)
    register_only = {key: value for key, value in injected.items() if key in {"http_post", "out", "err", "config_path"}}
    return run_register(args.server, args.token, args.device_id, allow_insecure=args.allow_insecure, **register_only)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
