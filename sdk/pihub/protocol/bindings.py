"""Binding object.

A binding identifies the widget-related endpoint a message refers to,
using the canonical `deviceId.programId.widgetId` identifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class Binding:
    """Identifies a runtime endpoint: a widget exposed by a program."""

    program_id: str
    widget_id: str
    device_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "deviceId": self.device_id,
            "programId": self.program_id,
            "widgetId": self.widget_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Binding":
        missing = [f for f in ("deviceId", "programId", "widgetId") if f not in data]
        if missing:
            raise ValueError(
                f"binding missing required field(s): {', '.join(missing)}"
            )
        return cls(
            program_id=data["programId"],
            widget_id=data["widgetId"],
            device_id=data["deviceId"],
        )

    @property
    def canonical_id(self) -> str:
        """The `deviceId.programId.widgetId` canonical identifier."""
        return f"{self.device_id}.{self.program_id}.{self.widget_id}"

    @classmethod
    def from_canonical_id(cls, canonical_id: str) -> "Binding":
        parts = canonical_id.split(".")
        if len(parts) != 3:
            raise ValueError(
                "canonical id must have exactly 3 '.'-separated components: "
                "deviceId.programId.widgetId"
            )
        device_id, program_id, widget_id = parts
        return cls(program_id=program_id, widget_id=widget_id, device_id=device_id)
