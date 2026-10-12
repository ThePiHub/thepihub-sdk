"""The built-in "Device Information" program: facts and health readings about the machine, shown on the dashboard.

A real device (one that built its own connection with `pihub setup`) gets this section automatically, first in its manifest, so every
dashboard has the same panel. It is an ORDINARY section: the fields below
are added with the same `section.output(...)` calls a device's own code uses, and that code can add its own the same way, or take one away:

    info = device.information                       # the section, with the standard fields already in it (before `device.start()`)
    info.output(id="battery", type="Gauge", read=read_battery, refresh="live")
    info.remove("wifi-signal")

Nothing in a device's own code is needed for the standard fields, and nothing here is specific to a Raspberry Pi: each field is registered **only if its source exists on this machine**
(probed once, at start, because a widget cannot appear or vanish after registration), so a plain Linux server simply gets fewer fields.

    Device()                                   # a real device: Device Information is on
    Device(device_information=False)           # off
    Device(device_information=DeviceInformation(show_ip_address=True))     # on, with the optional IP address

Two kinds of field:

* **static** fields (Pi model, hostname, OS, SDK version, device id, camera sensor) are published once, after `device.start()`;
* **live** fields (uptime, CPU, temperature, memory, disk, power, Wi-Fi signal) are read every `sample_seconds` **only while someone is
  viewing the device** (the first-viewer and last-viewer events), so an idle device and the platform are not fed readings nobody sees.

Nothing here reads the serial number or machine id (identifying, and already sent at registration), and nothing here can change the
machine: it is read-only.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from pihub.device_alerts import DEFAULT_RULES, AlertRule, DeviceAlerts
from pihub.program import Program
from pihub.properties.string import StringProperty

logger = logging.getLogger("pihub.device_information")

DEVICE_INFORMATION_PROGRAM_ID = "device-information"
DEVICE_INFORMATION_PROGRAM_NAME = "Device Information"
SAMPLE_SECONDS = 2.0

STATUS = "Status"
GAUGE = "Gauge"

# `vcgencmd get_throttled` bits. The low bits are "right now", the high ones "since boot".
_UNDER_VOLTAGE_NOW = 1 << 0
_FREQUENCY_CAPPED_NOW = 1 << 1
_THROTTLED_NOW = 1 << 2
_SOFT_TEMPERATURE_LIMIT_NOW = 1 << 3
_CURRENT_PROBLEMS = (
    (_UNDER_VOLTAGE_NOW, "under-voltage (check the power supply and cable)"),
    (_FREQUENCY_CAPPED_NOW, "the ARM frequency is capped"),
    (_THROTTLED_NOW, "the CPU is throttled"),
    (_SOFT_TEMPERATURE_LIMIT_NOW, "the temperature limit is active"),
)


@dataclass(frozen=True)
class InformationField:
    """One row of the panel. `kind` is "static" (published once) or "live" (sampled while viewed)."""

    id: str
    title: str
    widget: str
    kind: str
    units: Optional[str] = None


FIELDS: Tuple[InformationField, ...] = (
    InformationField("device-id", "Device ID", STATUS, "static"),
    InformationField("pi-model", "Pi model", STATUS, "static"),
    InformationField("hostname", "Hostname", STATUS, "static"),
    InformationField("operating-system", "Operating system", STATUS, "static"),
    InformationField("sdk-version", "SDK version", STATUS, "static"),
    InformationField("camera-sensor", "Camera sensor", STATUS, "static"),
    InformationField("ip-address", "IP address", STATUS, "static"),
    InformationField("uptime", "Uptime", STATUS, "live"),
    InformationField("cpu-usage", "CPU usage", GAUGE, "live", "%"),
    InformationField("cpu-temperature", "CPU temperature", GAUGE, "live", "°C"),
    InformationField("memory-used", "Memory used", GAUGE, "live", "%"),
    InformationField("disk-used", "Disk used", GAUGE, "live", "%"),
    InformationField("power", "Power supply", STATUS, "live"),
    InformationField("wifi-signal", "Wi-Fi signal", GAUGE, "live", " dBm"),
)
_BY_ID = {f.id: f for f in FIELDS}


def default_run(command: List[str]) -> Optional[str]:
    """Run a command and return its output, or None if it is missing, fails, or takes more than a couple of seconds."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


@dataclass
class Sources:
    """Where the readings come from. The defaults are this machine; a test points them at a fake tree."""

    proc: Path = Path("/proc")
    sys: Path = Path("/sys")
    etc: Path = Path("/etc")
    root: str = "/"  # the filesystem whose usage is reported
    run: Callable[[List[str]], Optional[str]] = default_run
    hostname: Callable[[], str] = platform.node
    machine: Callable[[], str] = platform.machine


class CpuMeter:
    """CPU busy share between two readings. The baseline is reset when a viewer arrives, so the first number after an idle gap is not
    an average over the whole gap."""

    def __init__(self, read: Callable[[], Optional[Tuple[int, int]]]) -> None:
        self._read = read
        self._previous: Optional[Tuple[int, int]] = None

    def reset(self) -> None:
        self._previous = self._read()

    def percent(self) -> Optional[float]:
        current = self._read()
        previous, self._previous = self._previous, current
        if current is None or previous is None or current[1] <= previous[1]:
            return None
        return round(100.0 * (current[0] - previous[0]) / (current[1] - previous[1]), 1)

    def __call__(self) -> Optional[float]:
        """So a meter can be handed to `Section.output(read=...)` as it is: the section calls `reset()` when sampling starts, then this."""
        return self.percent()


class Readings:
    """Every reading, each returning None when its source is not on this machine. Nothing here raises."""

    def __init__(self, sources: Optional[Sources] = None) -> None:
        self.sources = sources or Sources()

    def _text(self, path: Path) -> str:
        try:
            return path.read_text()
        except (OSError, UnicodeDecodeError):
            return ""

    def pi_model(self) -> Optional[str]:
        model = self._text(self.sources.proc / "device-tree" / "model").strip("\x00\n ")
        return model or None

    def hostname(self) -> Optional[str]:
        try:
            return self.sources.hostname() or None
        except Exception:
            return None

    def operating_system(self) -> Optional[str]:
        machine = self.sources.machine()
        for line in self._text(self.sources.etc / "os-release").splitlines():
            if line.startswith("PRETTY_NAME="):
                name = line.split("=", 1)[1].strip().strip('"')
                if name:
                    return f"{name} ({machine})" if machine else name
        return platform.platform() or None

    def sdk_version(self) -> Optional[str]:
        try:
            from importlib.metadata import version

            return version("thepihub")
        except Exception:
            return None

    def uptime(self) -> Optional[str]:
        try:
            seconds = int(float(self._text(self.sources.proc / "uptime").split()[0]))
        except (IndexError, ValueError):
            return None
        days, rest = divmod(seconds, 86400)
        hours, rest = divmod(rest, 3600)
        minutes = rest // 60
        return (f"{days} d " if days else "") + f"{hours} h {minutes} min"

    def cpu_times(self) -> Optional[Tuple[int, int]]:
        """(busy, total) jiffies from the first line of /proc/stat."""
        lines = self._text(self.sources.proc / "stat").splitlines()
        try:
            fields = [int(x) for x in lines[0].split()[1:]]
            return sum(fields) - (fields[3] + fields[4]), sum(fields)
        except (IndexError, ValueError):
            return None

    def cpu_temperature(self) -> Optional[float]:
        try:
            return round(int(self._text(self.sources.sys / "class" / "thermal" / "thermal_zone0" / "temp").strip()) / 1000, 1)
        except ValueError:
            return None

    def memory_used_percent(self) -> Optional[float]:
        info: Dict[str, int] = {}
        for line in self._text(self.sources.proc / "meminfo").splitlines():
            key, _, rest = line.partition(":")
            try:
                info[key] = int(rest.split()[0])
            except (IndexError, ValueError):
                continue
        if "MemTotal" not in info or "MemAvailable" not in info or info["MemTotal"] <= 0:
            return None
        return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)

    def disk_used_percent(self) -> Optional[float]:
        try:
            usage = shutil.disk_usage(self.sources.root)
        except OSError:
            return None
        return round(100.0 * usage.used / usage.total, 1) if usage.total else None

    def power(self) -> Optional[Tuple[str, str]]:
        """("OK" or "Warning", what is wrong), from `vcgencmd get_throttled`; None if this is not a Pi or the tool is missing."""
        output = self.sources.run(["vcgencmd", "get_throttled"])
        if not output or "=" not in output:
            return None
        try:
            flags = int(output.split("=", 1)[1].strip(), 16)
        except ValueError:
            return None
        problems = [text for bit, text in _CURRENT_PROBLEMS if flags & bit]
        if problems:
            return "Warning", "; ".join(problems)
        return "OK", ""

    def wifi_signal_dbm(self) -> Optional[float]:
        """Signal level of the first wireless interface in /proc/net/wireless, or None (no Wi-Fi, or the level is unknown)."""
        for line in self._text(self.sources.proc / "net" / "wireless").splitlines()[2:]:
            _, _, rest = line.partition(":")
            parts = rest.split()
            try:
                level = float(parts[2].rstrip("."))
            except (IndexError, ValueError):
                continue
            return None if level <= -255 else level
        return None

    def ip_address(self) -> Optional[str]:
        """The address this machine would use to reach the internet. A UDP "connect" sends nothing."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent
                return probe.getsockname()[0]
        except OSError:
            return None

    def camera_sensor(self) -> Optional[str]:
        """The first camera's sensor name, from libcamera. Slow (it loads picamera2), so it is read on a background thread."""
        try:
            from pihub.camera_setup import detect_sensor

            return detect_sensor()
        except Exception:
            return None


@dataclass
class DeviceInformation:
    """Options for the built-in program. Pass an instance as `Device(device_information=...)`."""

    show_ip_address: bool = False  # optional: it is shown on the platform, so it is off unless asked for
    sample_seconds: float = SAMPLE_SECONDS
    # Warnings and errors for a hot or busy CPU, a full disk, low memory (see `pihub.device_alerts`): True for the defaults, False for none, or your own rules.
    alerts: Union[bool, Sequence[AlertRule]] = True
    alert_seconds: float = 60.0  # how often the device checks, whether or not anyone is viewing
    sources: Optional[Sources] = field(default=None, repr=False)  # for tests: fake /proc, /sys and commands


def add_linux_information(
    section: Program, device_id: Union[str, Callable[[], Optional[str]]], options: Optional[DeviceInformation] = None
) -> None:
    """Add the standard machine facts and health readings to `section`, with the SAME calls a device's own code would use (`section.output(...)`).

    Each field is added only if its source exists on this machine (probed once, here), and not if it was removed on purpose (`section.remove(id)`).
    The serial number and machine id are never fields.
    """
    options = options or DeviceInformation()
    read_device_id: Callable[[], Optional[str]] = device_id if callable(device_id) else (lambda: device_id)
    readings = Readings(options.sources)
    section.sample_seconds = options.sample_seconds
    cpu = CpuMeter(readings.cpu_times)
    last_power: Dict[str, Optional[str]] = {"status": None}

    def power() -> Optional[str]:
        """The power status, warning the user ONCE each time it turns bad (the status itself only says "Warning")."""
        state = readings.power()
        if state is None:
            return None
        status, problems = state
        if status == "Warning" and last_power["status"] != "Warning":
            try:
                section.warning(f"Power or heat problem on this device: {problems}.")
            except Exception:
                logger.debug("could not send the power warning", exc_info=True)
        last_power["status"] = status
        return status

    def power_present() -> Optional[str]:
        state = readings.power()
        return state[0] if state else None

    # field id -> (the reader the section calls, what to call once to see whether this machine has the source at all)
    sources: Dict[str, Tuple[Callable[[], Any], Callable[[], Any]]] = {
        "device-id": (read_device_id, lambda: True),  # always present: the id may not be known yet (a first start, still registering)
        "pi-model": (readings.pi_model, readings.pi_model),
        "hostname": (readings.hostname, readings.hostname),
        "operating-system": (readings.operating_system, readings.operating_system),
        "sdk-version": (readings.sdk_version, readings.sdk_version),
        "uptime": (readings.uptime, readings.uptime),
        "cpu-usage": (cpu, readings.cpu_times),
        "cpu-temperature": (readings.cpu_temperature, readings.cpu_temperature),
        "memory-used": (readings.memory_used_percent, readings.memory_used_percent),
        "disk-used": (readings.disk_used_percent, readings.disk_used_percent),
        "power": (power, power_present),
        "wifi-signal": (readings.wifi_signal_dbm, readings.wifi_signal_dbm),
    }
    if options.show_ip_address:
        sources["ip-address"] = (readings.ip_address, readings.ip_address)

    for spec in FIELDS:
        if spec.id not in sources or section.is_excluded(spec.id):
            continue  # the camera sensor is added with the camera; the IP address only on request; a removed field stays removed
        read, probe = sources[spec.id]
        if probe() is None:
            continue  # this machine has no such source: register nothing, since a widget cannot appear or vanish after registration
        properties = [StringProperty(name="title", label="Title", default=spec.title)]
        if spec.units is not None:
            properties.append(StringProperty(name="units", label="Units", default=spec.units))
        section.output(id=spec.id, type=spec.widget, properties=properties, read=read, refresh=spec.kind)

    _add_alerts(section, options, readings)


def _add_alerts(section: Program, options: DeviceInformation, readings: Readings) -> None:
    """Watch the vitals even when nobody is viewing, and warn the owner (see `pihub.device_alerts`). A rule for a reading this machine does not have is skipped."""
    if options.alerts is False:
        return
    rules = DEFAULT_RULES if options.alerts is True else tuple(options.alerts)
    readers: Dict[str, Callable[[], Optional[float]]] = {
        "cpu-temperature": readings.cpu_temperature,
        "memory-used": readings.memory_used_percent,
        "disk-used": readings.disk_used_percent,
    }
    meter = CpuMeter(readings.cpu_times)
    meter.reset()  # a baseline, so the first check is a real interval
    readers["cpu-usage"] = meter.percent
    alerts = DeviceAlerts(section, readers, rules)
    section.every(options.alert_seconds, alerts.check, name="alerts")


def add_camera_information(section: Program, options: Optional[DeviceInformation], camera: Any = None) -> None:
    """Add what a device with a real camera shows about it: the sensor. (`camera` is the video source; the camera's settings are added here too.)"""
    options = options or DeviceInformation()
    readings = Readings(options.sources)
    spec = next(f for f in FIELDS if f.id == "camera-sensor")
    if section.widget(spec.id) is None and not section.is_excluded(spec.id):
        section.output(
            id=spec.id, type=spec.widget, properties=[StringProperty(name="title", label="Title", default=spec.title)],
            read=lambda: readings.camera_sensor() or "Not detected", refresh="static", background=True,  # slow: it loads picamera2
            group="Camera",  # shown with the camera's settings (`VideoSource.add_settings` uses the same name)
        )


def add_camera_settings(section: Program, options: Optional[DeviceInformation], source: Any) -> None:
    """Add the settable components a video source describes (`source.add_settings`): Resolution, Frame rate, Bitrate and, for a NoIR-capable camera,
    "Camera is NoIR"; then those of each recorder of that source (`Recorder.add_settings`: the "Video" group)."""
    add = getattr(source, "add_settings", None)
    if not callable(add):
        return
    sensor = Readings((options or DeviceInformation()).sources).camera_sensor() if getattr(source, "real_camera", False) else None
    add(section, sensor=sensor)
    for recorder in getattr(source, "recorders", []):
        recorder.add_settings(section)


def build_information_section(device_id: Union[str, Callable[[], Optional[str]]], options: Optional[DeviceInformation] = None) -> Program:
    """The Device Information section, with the standard fields in it. `Device` calls this; a device's own code reaches it as `device.information`."""
    section = Program(DEVICE_INFORMATION_PROGRAM_ID, DEVICE_INFORMATION_PROGRAM_NAME)
    add_linux_information(section, device_id, options)
    return section
