"""Device Identity: the fingerprint a device collects about its host.

A device has a *logical* identity (the `deviceId`) and a *physical* identity -
the fingerprint this module collects automatically from the host, so the
platform can eventually recognise a device across reinstalls without any
manual input (collection must be automatic: users are never asked to enter
machine IDs or serial numbers).

    from pihub.device_identity import collect_fingerprint

    fingerprint = collect_fingerprint()
    fingerprint.to_dict()
    # {"deviceType": "linux", "hostname": "birdbox01",
    #  "machineId": "b3974e6d...", "serialNumber": None}

This is deliberately about identity, not authentication or authorisation
(though it is the foundation for both). It collects technical identifiers
only, never user data.

Collection is best-effort and a missing value never fails registration, so
every collector below returns
`None` rather than raising when its source is unavailable - a Linux
server with no `/proc/cpuinfo` Serial line, or any non-Linux host, still
produces a usable (partial) fingerprint.
"""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

# Fingerprint Components > sources, per the spec's per-field sections.
MACHINE_ID_PATH = Path("/etc/machine-id")
CPUINFO_PATH = Path("/proc/cpuinfo")
DEVICE_TREE_MODEL_PATH = Path("/proc/device-tree/model")
DOCKERENV_PATH = Path("/.dockerenv")
CGROUP_PATH = Path("/proc/1/cgroup")
DMI_PRODUCT_NAME_PATH = Path("/sys/class/dmi/id/product_name")

DEVICE_TYPE_RASPBERRY_PI = "raspberrypi"
DEVICE_TYPE_JETSON = "jetson"
DEVICE_TYPE_ORANGE_PI = "orange-pi"
DEVICE_TYPE_DOCKER = "docker"
DEVICE_TYPE_VM = "vm"
DEVICE_TYPE_LINUX = "linux"
DEVICE_TYPE_UNKNOWN = "unknown"

_VM_PRODUCT_NAME_SIGNATURES = ("virtualbox", "vmware", "kvm", "qemu", "virtual machine", "bochs")


@dataclass
class DeviceFingerprint:
    """A device's physical identity, per "Device Fingerprint".

    Exactly the fields the spec's "Fingerprint Collection API" example
    shows: `device_type` and `hostname` are always populated;
    `machine_id` and `serial_number` are `None` when their source isn't
    available on this host.
    """

    device_type: str
    hostname: str
    machine_id: Optional[str] = None
    serial_number: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """The wire shape from "Registration Payload": camelCase keys."""
        return {
            "deviceType": self.device_type,
            "hostname": self.hostname,
            "machineId": self.machine_id,
            "serialNumber": self.serial_number,
        }


def _read_text(path: Path) -> Optional[str]:
    """Best-effort file read: stripped contents, or None on any failure.

    Never raises - a missing/unreadable file is exactly the "Missing
    values must not fail registration" case, not an error.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def collect_machine_id() -> Optional[str]:
    """Per "Machine ID": the primary Linux identifier, highest priority.

    Source: `/etc/machine-id`. `None` when it doesn't exist (non-Linux
    hosts, or a Linux host without systemd).
    """
    return _read_text(MACHINE_ID_PATH) or None


def collect_serial_number() -> Optional[str]:
    """Per "Raspberry Pi Serial Number": hardware identity for Raspberry Pi.

    Source: the `Serial` line in `/proc/cpuinfo`. `None` on any device
    that doesn't expose one (i.e. everything but a Raspberry Pi).
    """
    cpuinfo = _read_text(CPUINFO_PATH)
    if not cpuinfo:
        return None

    for line in cpuinfo.splitlines():
        if line.lower().startswith("serial"):
            _, _, value = line.partition(":")
            value = value.strip()
            return value or None

    return None


def collect_hostname() -> str:
    """Per "Hostname": `socket.gethostname()` - human-readable identity."""
    return socket.gethostname()


def detect_device_type() -> str:
    """Best-effort device type detection, per "Device Type".

    Checked most-specific first: a Raspberry Pi / Jetson / Orange Pi
    board is identified by its device-tree model string even when also
    running inside a container, since "Device Type" per the spec
    describes the physical platform family, not the runtime layer on top
    of it. Docker and VM detection follow, then a generic "linux"
    fallback, then "unknown" for anything not running Linux at all.
    """
    model = _read_text(DEVICE_TREE_MODEL_PATH)
    if model:
        lowered = model.lower()
        if "raspberry pi" in lowered:
            return DEVICE_TYPE_RASPBERRY_PI
        if "jetson" in lowered:
            return DEVICE_TYPE_JETSON
        if "orange pi" in lowered:
            return DEVICE_TYPE_ORANGE_PI

    if DOCKERENV_PATH.exists():
        return DEVICE_TYPE_DOCKER

    cgroup = _read_text(CGROUP_PATH)
    if cgroup and "docker" in cgroup:
        return DEVICE_TYPE_DOCKER

    product_name = _read_text(DMI_PRODUCT_NAME_PATH)
    if product_name and any(signature in product_name.lower() for signature in _VM_PRODUCT_NAME_SIGNATURES):
        return DEVICE_TYPE_VM

    if platform.system() != "Linux":
        return DEVICE_TYPE_UNKNOWN

    return DEVICE_TYPE_LINUX


def collect_fingerprint() -> DeviceFingerprint:
    """Collect this host's device fingerprint, per "Fingerprint Collection API".

    Called automatically during device registration (see
    `pihub.registration.manifest_builder`) - "No developer intervention
    required." Each field is collected independently and best-effort, so
    a missing `machine_id` or `serial_number` never prevents the rest of
    the fingerprint (or registration itself) from succeeding.
    """
    return DeviceFingerprint(
        device_type=detect_device_type(),
        hostname=collect_hostname(),
        machine_id=collect_machine_id(),
        serial_number=collect_serial_number(),
    )
