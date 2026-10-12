from typing import List

import pytest

from pihub.device_config import DeviceConfig, save_device_config

# The id the platform "assigned" to the machine the examples run on, in the tests: the examples never name an id, they take
# the one in the saved config (or register to get one).
SAVED_DEVICE_ID = "example-device"


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)


@pytest.fixture
def transport() -> RecordingTransport:
    return RecordingTransport()


@pytest.fixture(autouse=True)
def saved_identity(tmp_path, monkeypatch) -> str:
    """A registered machine in a throwaway config folder, so `Device()` finds its id as it does after registration."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from pihub.device_config import default_config_path

    save_device_config(DeviceConfig(device_id=SAVED_DEVICE_ID, device_secret="secret", server="wss://example.invalid"), default_config_path())
    return SAVED_DEVICE_ID
