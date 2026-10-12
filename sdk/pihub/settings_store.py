"""Settings that survive a restart: a small JSON file beside `config.json`, keyed by `<section id>.<widget id>`.

A settable component (`Section.setting(...)`) saves the value a user chose on the dashboard here, and reads it back the next time the device starts.
It is a separate file from `config.json` for the same reason `camera.json` is: `config.json` holds the device secret and is rewritten whole whenever the
device registers again, while a setting is about the hardware or the program and should survive that. A service that runs with its own
`XDG_CONFIG_HOME` reads the `settings.json` in that folder.

Nothing here ever raises: a missing or damaged file is simply "nothing saved", and a value that cannot be written is reported by `set()` returning False.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from pihub.device_config import default_config_path


def default_settings_path() -> Path:
    """`settings.json` beside `config.json`: `~/.config/thepihub/settings.json`, honouring `$XDG_CONFIG_HOME`."""
    return default_config_path().with_name("settings.json")


class SettingsStore:
    """Read and write saved settings. The path is looked up on each use, so a test (or a service) can change `XDG_CONFIG_HOME` first."""

    _lock = threading.Lock()  # one process writes this file from several threads (each setting's command arrives on its own)

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_settings_path()

    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def has(self, key: str) -> bool:
        return key in self._read()

    def get(self, key: str, default: Any = None) -> Any:
        return self._read().get(key, default)

    def set(self, key: str, value: Any) -> bool:
        """Save one value, keeping the others. True if it was saved."""
        with self._lock:
            data = self._read()
            data[key] = value
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            except OSError:
                return False
        return True

    def remove(self, key: str) -> bool:
        with self._lock:
            data = self._read()
            if key not in data:
                return False
            del data[key]
            try:
                self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            except OSError:
                return False
        return True
