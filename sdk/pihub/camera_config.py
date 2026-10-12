"""The camera setting saved on this device: which libcamera colour tuning its camera needs.

A camera cannot tell the software whether it has an infrared filter. libcamera picks its colour tuning by the sensor's name, and a
NoIR module (no filter) reports the same name as a normal one, so it gets a tuning meant for a filtered camera and its picture is
strongly pink (measured on a Pi Zero W with a NoIR Camera Module v2: red 5.6x green). Its own tuning, `<sensor>_noir`, fixes that.
So this is a fact somebody has to say once, and `pihub camera setup` asks and saves it here:

    {"tuning": "imx219_noir"}        or        {"tuning": null}        (asked, and it is a normal camera)

Having a `tuning` key AT ALL means the question was answered. A file with only `{"warned": true}` means the user was told once that
the picture looks pink (see `pihub.video.colour`) but has not answered, so the warning is not sent again and the setup still asks.

It is a separate file from `config.json` (next to it, in the same folder) on purpose: `config.json` holds the device's secret and is
rewritten whole whenever the device is registered again, while this is about the hardware and should survive that. A service that
runs with its own `XDG_CONFIG_HOME` reads the `camera.json` in that folder, so run `pihub camera setup` with the same one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from pihub.device_config import default_config_path

# Where libcamera installs its tuning files: Pi Zero to Pi 4 use `vc4`, the Pi 5 uses `pisp`.
TUNING_DIRECTORIES: Sequence[str] = ("/usr/share/libcamera/ipa/rpi/vc4", "/usr/share/libcamera/ipa/rpi/pisp")


def default_camera_config_path() -> Path:
    """`camera.json` beside `config.json`: `~/.config/thepihub/camera.json`, honouring `$XDG_CONFIG_HOME`."""
    return default_config_path().with_name("camera.json")


@dataclass
class CameraState:
    tuning: Optional[str] = None  # the saved tuning, or None for "normal" or "not set"
    asked: bool = False  # `pihub camera setup` has been answered (the file has a "tuning" key, even null)
    warned: bool = False  # the user has been told once that the picture looks pink


def _read(target: Path) -> Dict[str, Any]:
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_camera_state(path: Optional[Path] = None) -> CameraState:
    """What is saved about the camera. Never raises: a missing or damaged file is a state of "nothing saved"."""
    data = _read(path if path is not None else default_camera_config_path())
    tuning = data.get("tuning")
    return CameraState(
        tuning=tuning if isinstance(tuning, str) and tuning else None,
        asked="tuning" in data,
        warned=data.get("warned") is True,
    )


def load_camera_tuning(path: Optional[Path] = None) -> Optional[str]:
    """The saved tuning name, or `None` if nothing is saved, it says "normal", or the file is unreadable. Never raises."""
    return load_camera_state(path).tuning


def save_camera_tuning(tuning: Optional[str], path: Optional[Path] = None) -> Path:
    """Save the tuning (`None` for a normal camera: it records that the question was answered). Returns the file written."""
    target = path if path is not None else default_camera_config_path()
    data = _read(target)
    data["tuning"] = tuning
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return target


def mark_camera_warned(path: Optional[Path] = None) -> bool:
    """Record that the user was told the picture looks pink, WITHOUT recording an answer. True if it was saved."""
    target = path if path is not None else default_camera_config_path()
    data = _read(target)
    data["warned"] = True
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        return False
    return True


def noir_tuning_for(sensor: str, directories: Sequence[str] = TUNING_DIRECTORIES) -> Optional[str]:
    """The NoIR tuning libcamera ships for this sensor (`imx219` -> `imx219_noir`), or `None` if there is none installed."""
    name = f"{sensor.strip().lower()}_noir"
    for directory in directories:
        if (Path(directory) / f"{name}.json").is_file():
            return name
    return None


class CameraTuningStore:
    """`camera.json`'s tuning, seen as the saved value of a Toggle ("Camera is NoIR"), so the dashboard and `pihub camera setup` share one answer.

    True is "the NoIR tuning is saved", anything else is False. Turning it off records "normal camera" (the question stays answered).
    It is a `SettingStore` (`get` and `set` for a key, which is ignored: there is one camera setting here).
    """

    def __init__(self, noir_tuning: str, path: Optional[Path] = None) -> None:
        self.noir_tuning = noir_tuning
        self._path = path

    def get(self, key: str, default: Any = None) -> Any:
        state = load_camera_state(self._path)
        return state.tuning == self.noir_tuning if state.asked or state.tuning else default

    def set(self, key: str, value: Any) -> bool:
        try:
            save_camera_tuning(self.noir_tuning if value else None, self._path)
        except OSError:
            return False
        return True
