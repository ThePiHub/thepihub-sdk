"""A settable component: it shows a value and lets the dashboard change it (Toggle, Select, Slider or Text).

Every other widget goes one way: an `Output` shows a value the device publishes, a `Command` receives an action the browser sends. A setting is BOTH:

* the device **publishes its current value** as a runtime value (so a page that opens later shows what is set now), and
* the browser **changes it** with a command that carries `values: {"value": ...}`.

The device checks every change itself (a Toggle must be a boolean, a Select one of its options, a Slider inside its range and snapped to its step, a Text
no longer than its `max_length` if one was set) and ignores anything else with a warning, whatever the dashboard sent. An accepted value is **saved**
(`pihub.settings_store`, or a store of your own) so it survives a restart, published, and passed to `apply`, if one was given.

Use it through `Section.setting(...)`; a `Setting` is not built by hand. Its registration is an ordinary widget entry with `["display", "command"]` capabilities
and a `value` property that carries the default, and, for a Select, the `options`, or, for a Slider, `min`, `max` and `step`, or, for a Text, `maxLength` if
set, in the property schema.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, List, Optional, Protocol

from pihub.protocol.messages import CAPABILITY_COMMAND, CAPABILITY_DISPLAY
from pihub.widgets.base import Widget

logger = logging.getLogger("pihub.setting")

TOGGLE = "Toggle"
SELECT = "Select"
SLIDER = "Slider"
TEXT = "Text"
SETTING_TYPES = (TOGGLE, SELECT, SLIDER, TEXT)

_MISSING = object()


class SettingHost(Protocol):
    """What a `Setting` needs from the section it belongs to."""

    id: str

    def _publish_runtime(self, widget_id: str, *, value: Any = None, values: Any = None) -> None: ...

    def warning(self, message: str) -> None: ...


class SettingStore(Protocol):
    """Where a setting's value is kept. `pihub.settings_store.SettingsStore` is the default; camera.json backs the NoIR toggle."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> bool: ...


@dataclass
class Setting(Widget):
    """See the module docstring."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_DISPLAY, CAPABILITY_COMMAND]

    default: Any = None
    options: Optional[List[Any]] = None  # a Select
    minimum: Optional[float] = None  # a Slider
    maximum: Optional[float] = None
    step: Optional[float] = None
    max_length: Optional[int] = None  # a Text
    apply: Optional[Callable[[Any], None]] = field(default=None, repr=False, compare=False)
    store: Optional[SettingStore] = field(default=None, repr=False, compare=False)
    store_key: str = ""
    _host: Optional[SettingHost] = field(default=None, init=False, repr=False, compare=False)
    _value: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.type not in SETTING_TYPES:
            raise ValueError(f"a setting is one of {', '.join(SETTING_TYPES)}, not {self.type!r}")
        if self.type == SELECT and not self.options:
            raise ValueError(f"setting {self.id!r}: a Select needs its options")
        if self.type == SLIDER and (self.minimum is None or self.maximum is None):
            raise ValueError(f"setting {self.id!r}: a Slider needs a minimum and a maximum")
        self._value = self.check(self.default)  # a default that is not valid is a programming error, so it raises here

    # -- the value -----------------------------------------------------------------------------------------------------

    @property
    def value(self) -> Any:
        """What is set now: the saved value if there is a valid one, else the default."""
        return self._value

    def check(self, value: Any) -> Any:
        """The value as this setting stores it, or `ValueError` if it is not a valid value for it."""
        if self.type == TOGGLE:
            if not isinstance(value, bool):
                raise ValueError(f"{self.id}: a Toggle is true or false, not {value!r}")
            return value
        if self.type == SELECT:
            if value not in (self.options or []):
                raise ValueError(f"{self.id}: {value!r} is not one of {self.options!r}")
            return value
        if self.type == TEXT:
            if not isinstance(value, str):
                raise ValueError(f"{self.id}: a Text is a string, not {value!r}")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValueError(f"{self.id}: {len(value)} characters is over the {self.max_length}-character limit")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{self.id}: a Slider is a number, not {value!r}")
        assert self.minimum is not None and self.maximum is not None
        if not self.minimum <= value <= self.maximum:
            raise ValueError(f"{self.id}: {value} is outside {self.minimum} to {self.maximum}")
        if self.step:
            snapped = self.minimum + round((value - self.minimum) / self.step) * self.step
            value = min(self.maximum, snapped)
        whole = all(float(n).is_integer() for n in (self.minimum, self.maximum, self.step or 1, value))
        return int(value) if whole else round(float(value), 6)

    def bind(self, host: SettingHost) -> None:
        """Attach the section, and load the saved value (an invalid or missing one leaves the default)."""
        self._host = host
        if self.store is not None:
            stored = self.store.get(self.store_key, _MISSING)
            if stored is not _MISSING:
                try:
                    self._value = self.check(stored)
                except ValueError:
                    logger.debug("ignoring an invalid saved value for %s: %r", self.id, stored)

    # -- changing it ---------------------------------------------------------------------------------------------------

    def set(self, value: Any) -> Any:
        """Change the value from code: check it, save it, publish it, then call `apply`. Raises `ValueError` if it is not valid."""
        checked = self.check(value)
        self._value = checked
        if self.store is not None:
            self.store.set(self.store_key, checked)
        self.publish()
        if self.apply is not None:
            self.apply(checked)
        return checked

    def publish(self) -> None:
        """Publish the current value. Never raises: not connected right now just means the next publish (or the reconnect's re-send) carries it."""
        if self._host is None:
            return
        try:
            self._host._publish_runtime(self.id, value=self._value)
        except Exception:
            logger.debug("could not publish %s", self.id, exc_info=True)

    def on_command(self, message: Any) -> None:
        """A command arrived for SOME widget with this id (routing is by widget id alone): act only if it was for this section and carries a value."""
        host = self._host
        if host is None or message.binding.program_id != host.id:
            return
        values = getattr(message, "values", None)
        if not isinstance(values, dict) or "value" not in values:
            return
        try:
            self.set(values["value"])
        except ValueError as error:
            host.warning(f"Setting not changed: {error}")
        except Exception as error:  # `apply` raised: the value IS saved, but whatever it was to do did not happen
            logger.debug("applying %s failed", self.id, exc_info=True)
            host.warning(f"Setting {self.id} was saved, but applying it failed: {error}")
