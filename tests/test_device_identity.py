"""Tests for `pihub.device_identity`, covering:

    Machine ID Present / Missing
    Serial Number Present / Missing
    Hostname Retrieval
    Device Type Detection

plus "Registration tests should verify: Fingerprint Included In
Registration Payload" - covered in tests/registration/test_manifest_builder.py
and tests/protocol/test_messages.py, since that's where registration and
the wire payload actually live.
"""

from __future__ import annotations

import socket

import pihub.device_identity as device_identity
from pihub.device_identity import (
    DEVICE_TYPE_DOCKER,
    DEVICE_TYPE_JETSON,
    DEVICE_TYPE_LINUX,
    DEVICE_TYPE_ORANGE_PI,
    DEVICE_TYPE_RASPBERRY_PI,
    DEVICE_TYPE_UNKNOWN,
    DEVICE_TYPE_VM,
    DeviceFingerprint,
    collect_fingerprint,
    collect_hostname,
    collect_machine_id,
    collect_serial_number,
    detect_device_type,
)


# --- DeviceFingerprint -------------------------------------------------------


def test_fingerprint_to_dict_uses_wire_camel_case_keys():
    fingerprint = DeviceFingerprint(
        device_type="raspberrypi",
        hostname="birdbox01",
        machine_id="b3974e6d1f8f4d53971c7a889af4a64f",
        serial_number="10000000abcdef12",
    )

    assert fingerprint.to_dict() == {
        "deviceType": "raspberrypi",
        "hostname": "birdbox01",
        "machineId": "b3974e6d1f8f4d53971c7a889af4a64f",
        "serialNumber": "10000000abcdef12",
    }


def test_fingerprint_to_dict_includes_none_for_missing_optional_fields():
    fingerprint = DeviceFingerprint(device_type="linux", hostname="server01")

    assert fingerprint.to_dict() == {
        "deviceType": "linux",
        "hostname": "server01",
        "machineId": None,
        "serialNumber": None,
    }


# --- Machine ID --------------------------------------------------------------


def test_machine_id_present(tmp_path, monkeypatch):
    machine_id_path = tmp_path / "machine-id"
    machine_id_path.write_text("b3974e6d1f8f4d53971c7a889af4a64f\n")
    monkeypatch.setattr(device_identity, "MACHINE_ID_PATH", machine_id_path)

    assert collect_machine_id() == "b3974e6d1f8f4d53971c7a889af4a64f"


def test_machine_id_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(device_identity, "MACHINE_ID_PATH", tmp_path / "does-not-exist")

    assert collect_machine_id() is None


def test_machine_id_missing_when_file_is_empty(tmp_path, monkeypatch):
    machine_id_path = tmp_path / "machine-id"
    machine_id_path.write_text("")
    monkeypatch.setattr(device_identity, "MACHINE_ID_PATH", machine_id_path)

    assert collect_machine_id() is None


# --- Serial Number -------------------------------------------------------------


def test_serial_number_present(tmp_path, monkeypatch):
    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_text("processor\t: 0\nHardware\t: BCM2835\nSerial\t\t: 10000000abcdef12\n")
    monkeypatch.setattr(device_identity, "CPUINFO_PATH", cpuinfo_path)

    assert collect_serial_number() == "10000000abcdef12"


def test_serial_number_missing_when_no_serial_line(tmp_path, monkeypatch):
    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_text("processor\t: 0\nmodel name\t: Intel\n")
    monkeypatch.setattr(device_identity, "CPUINFO_PATH", cpuinfo_path)

    assert collect_serial_number() is None


def test_serial_number_missing_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(device_identity, "CPUINFO_PATH", tmp_path / "does-not-exist")

    assert collect_serial_number() is None


# --- Hostname ------------------------------------------------------------------


def test_hostname_retrieval_uses_socket_gethostname():
    assert collect_hostname() == socket.gethostname()


# --- Device Type Detection -----------------------------------------------------


def _patch_all_sources(monkeypatch, tmp_path, *, model=None, dockerenv=False, cgroup=None, dmi=None):
    device_tree_model_path = tmp_path / "model"
    if model is not None:
        device_tree_model_path.write_text(model)
    monkeypatch.setattr(device_identity, "DEVICE_TREE_MODEL_PATH", device_tree_model_path)

    dockerenv_path = tmp_path / "dockerenv"
    if dockerenv:
        dockerenv_path.write_text("")
    monkeypatch.setattr(device_identity, "DOCKERENV_PATH", dockerenv_path)

    cgroup_path = tmp_path / "cgroup"
    if cgroup is not None:
        cgroup_path.write_text(cgroup)
    monkeypatch.setattr(device_identity, "CGROUP_PATH", cgroup_path)

    dmi_path = tmp_path / "product_name"
    if dmi is not None:
        dmi_path.write_text(dmi)
    monkeypatch.setattr(device_identity, "DMI_PRODUCT_NAME_PATH", dmi_path)


def test_detects_raspberry_pi_from_device_tree_model(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, model="Raspberry Pi 4 Model B Rev 1.4")

    assert detect_device_type() == DEVICE_TYPE_RASPBERRY_PI


def test_detects_jetson_from_device_tree_model(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, model="NVIDIA Jetson Nano Developer Kit")

    assert detect_device_type() == DEVICE_TYPE_JETSON


def test_detects_orange_pi_from_device_tree_model(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, model="Orange Pi 5")

    assert detect_device_type() == DEVICE_TYPE_ORANGE_PI


def test_detects_docker_via_dockerenv_file(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, dockerenv=True)

    assert detect_device_type() == DEVICE_TYPE_DOCKER


def test_detects_docker_via_cgroup(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, cgroup="12:pids:/docker/abc123\n")

    assert detect_device_type() == DEVICE_TYPE_DOCKER


def test_detects_vm_from_dmi_product_name(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, dmi="VirtualBox")

    assert detect_device_type() == DEVICE_TYPE_VM


def test_falls_back_to_linux_when_nothing_matches(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(device_identity.platform, "system", lambda: "Linux")

    assert detect_device_type() == DEVICE_TYPE_LINUX


def test_falls_back_to_unknown_on_non_linux_platforms(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(device_identity.platform, "system", lambda: "Windows")

    assert detect_device_type() == DEVICE_TYPE_UNKNOWN


def test_raspberry_pi_model_wins_even_inside_a_container(tmp_path, monkeypatch):
    """A Pi running inside a container is still reported as its physical board."""
    _patch_all_sources(monkeypatch, tmp_path, model="Raspberry Pi 4 Model B Rev 1.4", dockerenv=True)

    assert detect_device_type() == DEVICE_TYPE_RASPBERRY_PI


# --- collect_fingerprint() ------------------------------------------------------


def test_collect_fingerprint_combines_all_four_collectors(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path, model="Raspberry Pi 4 Model B Rev 1.4")

    machine_id_path = tmp_path / "machine-id"
    machine_id_path.write_text("b3974e6d1f8f4d53971c7a889af4a64f")
    monkeypatch.setattr(device_identity, "MACHINE_ID_PATH", machine_id_path)

    cpuinfo_path = tmp_path / "cpuinfo"
    cpuinfo_path.write_text("Serial\t\t: 10000000abcdef12\n")
    monkeypatch.setattr(device_identity, "CPUINFO_PATH", cpuinfo_path)

    monkeypatch.setattr(device_identity.socket, "gethostname", lambda: "birdbox01")

    fingerprint = collect_fingerprint()

    assert fingerprint == DeviceFingerprint(
        device_type=DEVICE_TYPE_RASPBERRY_PI,
        hostname="birdbox01",
        machine_id="b3974e6d1f8f4d53971c7a889af4a64f",
        serial_number="10000000abcdef12",
    )


def test_collect_fingerprint_never_raises_when_everything_is_missing(tmp_path, monkeypatch):
    _patch_all_sources(monkeypatch, tmp_path)
    monkeypatch.setattr(device_identity, "MACHINE_ID_PATH", tmp_path / "does-not-exist-machine-id")
    monkeypatch.setattr(device_identity, "CPUINFO_PATH", tmp_path / "does-not-exist-cpuinfo")

    fingerprint = collect_fingerprint()  # should not raise

    assert fingerprint.machine_id is None
    assert fingerprint.serial_number is None
    assert fingerprint.hostname == socket.gethostname()
