"""One program at a time per saved device identity.

Two programs connecting as the same device do not add up to one device: the platform keeps one connection and one
list of programs per device, so the second displaces the first and they can fight forever. This is the simple guard for the case that actually happens: two programs on one machine
using one saved identity. The running program holds an exclusive `flock` on `device.lock`, beside `config.json`; the
operating system drops it when the process exits or crashes, so it can never be stale.

A second program does not connect. It raises `DeviceInUseError` with a message naming the holder. A boot service
(`PIHUB_SERVICE` is set in its unit) instead waits, quietly, and takes over as soon as the other program ends.

Not covered, on purpose: the same identity copied to another machine (the server would have to notice), and
platforms without `fcntl` or a filesystem without locks: there the guard is skipped, never an error.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

SERVICE_ENV = "PIHUB_SERVICE"
WAIT_SECONDS = 5.0


class DeviceInUseError(RuntimeError):
    """Another program on this machine is already connected as this device."""


def lock_path_for(config_path: Path) -> Path:
    return config_path.with_name("device.lock")


def _holder_text(info: Dict[str, Any]) -> str:
    parts = []
    if info.get("script"):
        parts.append(str(info["script"]))
    if info.get("pid"):
        parts.append(f"pid {info['pid']}")
    what = ", ".join(parts) or "another program"
    service = info.get("service")
    return f"{what}, the service pihub-{service}" if service else what


def _in_use_message(device_label: str, info: Dict[str, Any]) -> str:
    stop = (
        f"Stop it with `systemctl --user stop pihub-{info['service']}`"
        if info.get("service")
        else "Stop it (Ctrl+C in its terminal)"
    )
    return (
        f"Another program is already connected as {device_label}: {_holder_text(info)}. "
        f"Two programs cannot share one device identity at the same time. {stop}, or give this program its own "
        "identity by running it with XDG_CONFIG_HOME=$HOME/.config-mine (it will ask you to link it as a new device)."
    )


class DeviceLock:
    """Holds the lock for as long as it is open. `acquire()` returns one, or raises `DeviceInUseError`."""

    def __init__(self, path: Path, handle: Any) -> None:
        self.path = path
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()  # closing drops the flock
        except OSError:  # pragma: no cover
            pass

    @property
    def held(self) -> bool:
        return self._handle is not None


def _read_holder(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text() or "{}")
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def acquire(
    config_path: Path,
    device_label: str,
    *,
    wait: Optional[bool] = None,
    sleep: Callable[[float], None] = time.sleep,
    script: Optional[str] = None,
) -> Optional[DeviceLock]:
    """Take the identity's lock, or return `None` when locking is not possible here (no `fcntl`, or a filesystem that
    refuses locks). Raises `DeviceInUseError` when another program holds it, unless `wait` (default: this is a boot
    service, `PIHUB_SERVICE` is set) - then it waits for it, logging once."""
    if fcntl is None:
        return None
    service = os.environ.get(SERVICE_ENV) or None
    if wait is None:
        wait = service is not None

    path = lock_path_for(config_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")  # non-inheritable (PEP 446): a child process never keeps the lock alive
    except OSError:
        logger.debug("could not open %s: the one-program-at-a-time guard is off", path, exc_info=True)
        return None

    announced = False
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as error:
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                handle.close()
                logger.debug("locking %s is not supported here: the guard is off", path, exc_info=True)
                return None
            info = _read_holder(path)
            if not wait:
                handle.close()
                raise DeviceInUseError(_in_use_message(device_label, info)) from None
            if not announced:
                logger.warning(
                    "%s is in use by %s; waiting for it to stop, then this service takes over",
                    device_label, _holder_text(info),
                )
                announced = True
            sleep(WAIT_SECONDS)

    try:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "script": os.path.abspath(name) if (name := (script if script is not None else sys.argv[0])) else None,
            "service": service,
            "since": time.time(),
        }))
        handle.flush()
    except OSError:  # pragma: no cover - the lock is held; only the holder's description is missing
        logger.debug("could not describe the lock holder", exc_info=True)
    return DeviceLock(path, handle)
