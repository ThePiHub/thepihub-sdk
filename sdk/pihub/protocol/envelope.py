"""Protocol envelope.

Every message uses the same envelope structure:

    {
      "protocolVersion": 1,
      "messageType": "runtime",
      "timestamp": "2026-09-14T18:00:00Z",
      "payload": {}
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

CURRENT_PROTOCOL_VERSION = 1

_ENVELOPE_FIELDS = ("protocolVersion", "messageType", "timestamp", "payload")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Envelope:
    """The common protocol envelope shared by every message type."""

    message_type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    protocol_version: int = CURRENT_PROTOCOL_VERSION
    timestamp: str = field(default_factory=_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "protocolVersion": self.protocol_version,
            "messageType": self.message_type,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Envelope":
        """Validate and construct an Envelope from a decoded message dict.

        Per the protocol's error handling rules, this validates the
        envelope structure itself (required fields and their types).
        Unknown top-level fields are ignored rather than rejected.
        """
        if not isinstance(data, dict):
            raise ValueError("envelope must be an object")

        missing = [f for f in _ENVELOPE_FIELDS if f not in data]
        if missing:
            raise ValueError(
                f"envelope missing required field(s): {', '.join(missing)}"
            )

        protocol_version = data["protocolVersion"]
        if not isinstance(protocol_version, int) or isinstance(protocol_version, bool):
            raise ValueError("envelope 'protocolVersion' must be an integer")

        message_type = data["messageType"]
        if not isinstance(message_type, str):
            raise ValueError("envelope 'messageType' must be a string")

        timestamp = data["timestamp"]
        if not isinstance(timestamp, str):
            raise ValueError("envelope 'timestamp' must be a string")

        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ValueError("envelope 'payload' must be an object")

        return cls(
            message_type=message_type,
            payload=payload,
            protocol_version=protocol_version,
            timestamp=timestamp,
        )

    @classmethod
    def from_json(cls, data: str) -> "Envelope":
        return cls.from_dict(json.loads(data))
