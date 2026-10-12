"""Alerts from the machine's own vitals: a hot CPU, a busy CPU, a full disk, low memory.

Device Information shows these readings while someone is viewing. An alert has to reach the owner when NOBODY is, so the device checks them itself,
slowly (once a minute by default, from `Program.every`, so an idle device costs almost nothing), and sends a notification from Device Information:

* a **warning** when a reading passes its warning level, an **error** when it passes its error level (the dashboard asks the user to acknowledge both);
* **once per episode**, not once per reading: a reading that stays high says nothing more. It has to fall a margin BELOW the level (hysteresis) before it
  counts as recovered, so a reading hovering around the level cannot flap;
* an **information** notice when it has recovered ("CPU temperature is back to normal"), which needs nothing from the user.

A reading can also be required to stay over its level for several checks in a row (`sustained`) before it counts, so a brief CPU spike is not an alert.

The levels are provisional: they are a first guess for a Raspberry Pi, to be tuned from use. Change them with `DeviceInformation(alerts=[AlertRule(...)])`, or turn
the whole thing off with `DeviceInformation(alerts=False)`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger("pihub.device_alerts")

OK, WARNING, ERROR = 0, 1, 2


@dataclass(frozen=True)
class AlertRule:
    """When one Device Information reading (`field`, e.g. "cpu-temperature") becomes a warning, an error, and recovered.

    `units` is shown after the number as written ("%", " °C"). `clear_margin` is how far below a level the reading must fall before it stops
    counting as over it. `sustained` is how many checks in a row must be over a level before it raises (1: at once).
    """

    field: str
    warn_at: float
    error_at: float
    clear_margin: float
    high: str  # said at the warning level, e.g. "CPU temperature is high"
    critical: str  # said at the error level
    recovered: str  # said when it is back to normal
    units: str = ""
    sustained: int = 1

    def __post_init__(self) -> None:
        if not self.warn_at < self.error_at:
            raise ValueError(f"{self.field}: warn_at must be below error_at")
        if self.clear_margin < 0:
            raise ValueError(f"{self.field}: clear_margin cannot be negative")
        if self.sustained < 1:
            raise ValueError(f"{self.field}: sustained must be at least 1")


DEFAULT_RULES = (
    AlertRule("cpu-temperature", 70.0, 80.0, 5.0, "CPU temperature is high", "CPU temperature is critical", "CPU temperature is back to normal", " °C"),
    AlertRule(
        "cpu-usage", 90.0, 98.0, 10.0, "CPU usage has stayed high", "CPU usage has stayed very high", "CPU usage is back to normal", "%", sustained=5,
    ),
    AlertRule("memory-used", 90.0, 97.0, 5.0, "Memory is nearly full", "Memory is almost exhausted", "Memory use is back to normal", "%"),
    AlertRule("disk-used", 85.0, 95.0, 3.0, "Disk is getting full", "Disk is nearly full", "Disk space is back to normal", "%"),
)


class _State:
    def __init__(self) -> None:
        self.level = OK
        self.streak = 0  # consecutive checks that pointed above the current level


class DeviceAlerts:
    """Checks readings against rules and notifies, once per episode. `readers` maps a rule's field to a function returning the reading (or None: no reading)."""

    def __init__(self, section: Any, readers: Dict[str, Callable[[], Optional[float]]], rules: Sequence[AlertRule] = DEFAULT_RULES) -> None:
        self._section = section
        self._readers = readers
        self._rules = [rule for rule in rules if rule.field in readers]  # a machine without the source has nothing to check
        self._state = {rule.field: _State() for rule in self._rules}

    def check(self) -> None:
        """Take one reading of each rule and send whatever has changed. Never raises."""
        for rule in self._rules:
            try:
                value = self._readers[rule.field]()
            except Exception:
                logger.debug("could not read %s", rule.field, exc_info=True)
                continue
            if value is None:
                continue
            self._step(rule, value)

    @staticmethod
    def _target(rule: AlertRule, level: int, value: float) -> int:
        """The level this reading points to. Once at a level, the reading must fall `clear_margin` below it to leave it (hysteresis)."""
        if value >= rule.error_at or (level >= ERROR and value >= rule.error_at - rule.clear_margin):
            return ERROR
        if value >= rule.warn_at or (level >= WARNING and value >= rule.warn_at - rule.clear_margin):
            return WARNING
        return OK

    def _step(self, rule: AlertRule, value: float) -> None:
        state = self._state[rule.field]
        target = self._target(rule, state.level, value)
        if target > state.level:
            state.streak += 1
            if state.streak < rule.sustained:
                return
            state.level, state.streak = target, 0
            self._send("error" if target == ERROR else "warning", rule.critical if target == ERROR else rule.high, rule, value)
        elif target < state.level:
            state.level, state.streak = target, 0
            if target == OK:
                self._send("info", rule.recovered, rule, value)
            # ERROR -> WARNING says nothing: the problem is easing, not over
        else:
            state.streak = 0

    def _send(self, severity: str, phrase: str, rule: AlertRule, value: float) -> None:
        message = f"{phrase}: {value:g}{rule.units}"
        try:
            getattr(self._section, severity)(message)
        except Exception:  # not connected right now: this episode is not re-announced, but the readings on the dashboard show it
            logger.debug("could not send the %s notification for %s", severity, rule.field, exc_info=True)
