"""Device configuration persistence: the device configuration model.

Stores exactly what a device needs to reconnect without repeating the
one-time registration-token exchange:

    {
      "deviceId": "birdbox01",
      "deviceSecret": "...",
      "server": "ws://localhost:8000"
    }

`Device.register()` writes this; `Device.start()` reads it back (see
device.py) to authenticate automatically on subsequent runs.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass
class DeviceConfig:
    """A device's persisted identity: which server it belongs to and how
    to authenticate with it."""

    device_id: str
    device_secret: str
    server: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "deviceSecret": self.device_secret,
            "server": self.server,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceConfig":
        return cls(
            device_id=data["deviceId"],
            device_secret=data["deviceSecret"],
            server=data["server"],
        )


def default_config_path() -> Path:
    """`~/.config/thepihub/config.json`, honouring `$XDG_CONFIG_HOME` if set."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "thepihub" / "config.json"


def save_device_config(config: DeviceConfig, path: Optional[Path] = None) -> None:
    """Persist `config` as JSON, creating parent directories as needed.

    Chmod'd to owner-read/write only (0600) since the file contains a
    device secret - best-effort, since not every filesystem supports
    POSIX permissions.
    """
    target = path if path is not None else default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.to_dict(), indent=2))
    try:
        os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_device_config(path: Optional[Path] = None) -> Optional[DeviceConfig]:
    """Load a previously saved config, or `None` if it doesn't exist,
    isn't readable, or isn't valid - never raises."""
    target = path if path is not None else default_config_path()
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return None

    try:
        return DeviceConfig.from_dict(data)
    except (KeyError, TypeError):
        return None
