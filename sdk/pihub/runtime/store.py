"""RuntimeStore: a cache of the latest runtime values.

On the platform the Runtime Store lives in the browser (a Pinia store fed
by the connection and read by widgets), not on the device. This module is a
standalone port of its mechanics (not wired into `Device`/`Program`, which
don't need it): the cache semantics, key structure and API described below
hold regardless of which side of the wire the store sits on.

Rule 1: the store is a cache, not the source of truth -
the device remains authoritative; this class only remembers the latest
value(s) it was told about via `update()`.

Rule 2: all runtime values are indexed by `deviceId.programId.widgetId`
- the existing `Binding.canonical_id` (see protocol/bindings.py) already
implements exactly this, so keys are derived from it rather than
re-deriving the same format here.

The store's API takes a binding object in `update(binding, value)` but plain
strings in `get("greenhouse01.weather.temperature")` /
`clearDevice("greenhouse01")` - so `update()`,
`get()` and `remove()` accept either a `Binding` or an already-canonical
string key here, so callers can use whichever they have on hand.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Union

from pihub.protocol.bindings import Binding
from pihub.runtime.value import RuntimeValue

RuntimeKey = Union[Binding, str]


def _canonical_key(key: RuntimeKey) -> str:
    return key.canonical_id if isinstance(key, Binding) else key


class RuntimeStore:
    """A cache of the latest runtime value(s), keyed by `deviceId.programId.widgetId`."""

    def __init__(self) -> None:
        self._values: Dict[str, RuntimeValue] = {}

    def update(
        self,
        binding: RuntimeKey,
        value: Any = None,
        *,
        values: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> RuntimeValue:
        """Cache a new single or multi runtime value for `binding`.

        Per "Runtime Store API": `runtimeStore.update(binding, value)`.
        Overwrites whatever was previously cached for this key - the
        latest update always wins.
        """
        entry = RuntimeValue(value=value, values=values, timestamp=timestamp)
        self._values[_canonical_key(binding)] = entry
        return entry

    def get(self, binding: RuntimeKey) -> Optional[RuntimeValue]:
        """The latest cached value for `binding`, or None if not cached.

        Per "Runtime Store API": `runtimeStore.get("greenhouse01.weather.temperature")`.
        """
        return self._values.get(_canonical_key(binding))

    def remove(self, binding: RuntimeKey) -> None:
        """Remove one endpoint's cached value. A no-op if it isn't cached."""
        self._values.pop(_canonical_key(binding), None)

    def clear(self) -> None:
        """Remove every cached value."""
        self._values.clear()

    def clear_device(self, device_id: str) -> None:
        """Remove every cached value belonging to `device_id`.

        Per "Runtime Store API": `runtimeStore.clearDevice("greenhouse01")`
        - "Useful when a device disconnects." A no-op if none are cached.
        """
        stale_keys = [
            key for key in self._values if Binding.from_canonical_id(key).device_id == device_id
        ]
        for key in stale_keys:
            del self._values[key]
