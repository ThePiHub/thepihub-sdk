"""What one device can carry, announced to the platform."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class DeviceLimits:
    """A device's own capacity limits.

    The SDK's defaults are the recommended values. A
    developer overrides them per device: a Jetson with a hardware encoder can
    raise them, a Pi Zero should lower them. The platform enforces the smaller
    of these and the account's tier limit - and the device may still refuse,
    and its refusal wins.

    Only `max_viewer_sessions` is enforced today; the other two are announced
    now so the wire format does not change when video and files arrive.
    """

    max_viewer_sessions: int = 5
    max_media_sessions: int = 1
    max_file_transfers: int = 2

    def to_dict(self) -> Dict[str, int]:
        return {
            "maxViewerSessions": self.max_viewer_sessions,
            "maxMediaSessions": self.max_media_sessions,
            "maxFileTransfers": self.max_file_transfers,
        }
