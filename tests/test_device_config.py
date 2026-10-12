"""Tests for `pihub.device_config`: the device configuration model, and saving and loading the config."""

from __future__ import annotations

import stat

from pihub.device_config import DeviceConfig, default_config_path, load_device_config, save_device_config


def test_load_returns_none_when_the_file_does_not_exist(tmp_path):
    assert load_device_config(tmp_path / "config.json") is None


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")

    save_device_config(config, path)
    loaded = load_device_config(path)

    assert loaded == config


def test_save_writes_the_documented_wire_shape(tmp_path):
    path = tmp_path / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")

    save_device_config(config, path)

    import json

    assert json.loads(path.read_text()) == {
        "deviceId": "birdbox01",
        "deviceSecret": "s3cr3t",
        "server": "ws://localhost:8000",
    }


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")

    save_device_config(config, path)

    assert path.exists()


def test_save_restricts_file_permissions_to_owner_only(tmp_path):
    path = tmp_path / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")

    save_device_config(config, path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == stat.S_IRUSR | stat.S_IWUSR


def test_save_tolerates_a_filesystem_that_rejects_chmod(tmp_path, monkeypatch):
    """Not every filesystem supports POSIX permissions - `save_device_config`
    must still succeed (the file itself is written either way)."""
    path = tmp_path / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")

    def raise_os_error(*args, **kwargs):
        raise OSError("chmod not supported")

    monkeypatch.setattr("pihub.device_config.os.chmod", raise_os_error)

    save_device_config(config, path)  # should not raise

    assert load_device_config(path) == config


def test_load_returns_none_for_malformed_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not valid json")

    assert load_device_config(path) is None


def test_load_returns_none_when_required_fields_are_missing(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"deviceId": "birdbox01"}')

    assert load_device_config(path) is None


def test_default_config_path_honours_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "thepihub" / "config.json"


def test_default_config_path_falls_back_to_home(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr("pihub.device_config.Path.home", lambda: tmp_path)
    assert default_config_path() == tmp_path / ".config" / "thepihub" / "config.json"


def test_restart_then_reconnect_reads_back_the_same_config(tmp_path):
    """Simulates a device process restarting: save once, then two
    independent loads (as two separate process runs would do) see the
    same persisted identity."""
    path = tmp_path / "config.json"
    config = DeviceConfig(device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000")
    save_device_config(config, path)

    first_run = load_device_config(path)
    second_run = load_device_config(path)

    assert first_run == config
    assert second_run == config
