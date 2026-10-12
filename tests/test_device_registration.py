"""Tests for `Device.register()` and automatic authenticated reconnection,
covering:

    device.register("ABC123XY")

must result in the secret being stored, and a later `device.start()`
must authenticate automatically using it.
"""

from __future__ import annotations

from pihub import Device
from pihub.device_config import DeviceConfig, load_device_config


class RecordingHttpPost:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list = []

    def __call__(self, url: str, payload: dict) -> dict:
        self.calls.append((url, payload))
        return self.response


def test_register_persists_the_returned_secret(tmp_path):
    config_path = tmp_path / "config.json"
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    device = Device(id="birdbox01", config_path=config_path)

    device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    loaded = load_device_config(config_path)
    assert loaded == DeviceConfig(
        device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000"
    )


def test_register_returns_the_config(tmp_path):
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    device = Device(id="birdbox01", config_path=tmp_path / "config.json")

    config = device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    assert config == DeviceConfig(
        device_id="birdbox01", device_secret="s3cr3t", server="ws://localhost:8000"
    )


def test_register_sends_the_collected_fingerprint_automatically(tmp_path):
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    device = Device(id="birdbox01", config_path=tmp_path / "config.json")

    device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    _, payload = http_post.calls[0]
    assert payload["registrationToken"] == "ABC123XY"
    assert payload["deviceId"] == "birdbox01"
    assert set(payload["fingerprint"]) == {"deviceType", "hostname", "machineId", "serialNumber"}


def test_register_stores_the_server_as_a_websocket_url(tmp_path):
    config_path = tmp_path / "config.json"
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    device = Device(id="birdbox01", config_path=config_path)

    device.register("ABC123XY", server="https://platform.example.com", http_post=http_post)

    loaded = load_device_config(config_path)
    assert loaded.server == "wss://platform.example.com"


def test_start_with_no_transport_and_no_prior_registration_stays_transportless(tmp_path):
    device = Device(id="birdbox01", config_path=tmp_path / "does-not-exist.json")

    device.start()  # should not raise

    assert device._transport is None
    device.stop()


def test_build_authenticated_transport_uses_the_persisted_secret_and_server(tmp_path):
    """`_build_authenticated_transport()` is what `start()` calls when no
    explicit transport was given - tested directly here (rather than via
    a full `start()`) to check the URL it builds without needing a real
    server to actually connect to."""
    config_path = tmp_path / "config.json"
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    registering_device = Device(id="birdbox01", config_path=config_path)
    registering_device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    # A fresh Device instance, as a new process run would create - no
    # transport is passed explicitly.
    device = Device(id="birdbox01", config_path=config_path)

    transport = device._build_authenticated_transport()

    from pihub.transport.websocket import WebSocketTransport

    assert isinstance(transport, WebSocketTransport)
    # Per "WebRTC_Architecture.md" > 5.9 item 2 (Step 0c): the secret
    # travels in a header, never in the URL - a URL ends up in access
    # logs, proxy logs and error reports, and a device secret does not
    # expire on its own.
    assert transport._url == "ws://localhost:8000/ws/devices/birdbox01"
    assert transport._headers == {"Authorization": "Bearer s3cr3t"}


def test_start_ignores_a_config_registered_under_a_different_device_id(tmp_path):
    config_path = tmp_path / "config.json"
    http_post = RecordingHttpPost({"deviceId": "greenhouse01", "deviceSecret": "s3cr3t"})
    other_device = Device(id="greenhouse01", config_path=config_path)
    other_device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    device = Device(id="birdbox01", config_path=config_path)
    device.start()

    assert device._transport is None
    device.stop()


def test_an_explicitly_given_transport_is_never_overridden_by_a_persisted_config(tmp_path):
    config_path = tmp_path / "config.json"
    http_post = RecordingHttpPost({"deviceId": "birdbox01", "deviceSecret": "s3cr3t"})
    registering_device = Device(id="birdbox01", config_path=config_path)
    registering_device.register("ABC123XY", server="http://localhost:8000", http_post=http_post)

    class RecordingTransport:
        def __init__(self) -> None:
            self.sent: list = []

        def send(self, data: str) -> None:
            self.sent.append(data)

    explicit_transport = RecordingTransport()
    device = Device(id="birdbox01", transport=explicit_transport, config_path=config_path)

    device.start()

    assert device._transport is explicit_transport


def test_a_device_whose_id_does_not_match_the_saved_registration_says_so(tmp_path, caplog):
    """The trap: `Device(id="birdbox-live")` on a machine registered as
    another id connects to nothing, and used to say nothing about it."""
    import logging

    config_path = tmp_path / "config.json"
    save = __import__("pihub.device_config", fromlist=["save_device_config"]).save_device_config
    save(DeviceConfig("raspberrypi-abc123", "s3cr3t", "wss://platform.example.com"), config_path)
    device = Device(id="birdbox-live", config_path=config_path)

    with caplog.at_level(logging.WARNING, logger="pihub.device"):
        transport = device._build_authenticated_transport()

    assert transport is None
    assert "raspberrypi-abc123" in caplog.text
    assert "Device()" in caplog.text


def test_a_device_with_no_saved_registration_stays_quiet(tmp_path, caplog):
    """No config at all is the normal offline/simulation case, not a mistake."""
    import logging

    device = Device(id="birdbox01", config_path=tmp_path / "missing.json")

    with caplog.at_level(logging.WARNING, logger="pihub.device"):
        assert device._build_authenticated_transport() is None

    assert caplog.text == ""
