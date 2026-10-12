"""RuntimeValue: one entry of the runtime store, single or multi valued.

The store maps a canonical key to an entry:

    {
        "greenhouse01.weather.temperature": {
            "value": 22.5,
            "timestamp": 1720000000
        },
        "greenhouse01.weather.humidity": {
            "value": 64
        }
    }

A `RuntimeValue` is one cached entry: exactly one of `value` (single) or
`values` (multi) - mirroring the existing `RuntimeMessage` protocol
dataclass's own `value`/`values` duality (see protocol/messages.py) -
plus a `timestamp`. The device owns the values and the store owns only the
latest copy - this is a cache entry, not a source of truth.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RuntimeValue:
    """One cached runtime update: a single `value` or multi `values`, with a timestamp."""

    value: Optional[Any] = None
    values: Optional[Dict[str, Any]] = None
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        if (self.value is None) == (self.values is None):
            raise ValueError("RuntimeValue requires exactly one of 'value' or 'values'")
        if self.timestamp is None:
            self.timestamp = time.time()

    def to_dict(self) -> Dict[str, Any]:
        """This entry's `{"value": ..., "timestamp": ...}` (or `values`) shape."""
        payload: Dict[str, Any] = {}
        if self.value is not None:
            payload["value"] = self.value
        else:
            payload["values"] = self.values
        payload["timestamp"] = self.timestamp
        return payload
