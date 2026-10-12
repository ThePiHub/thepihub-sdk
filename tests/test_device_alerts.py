"""Alerts from the machine's vitals: one notification per episode, with hysteresis, and none from a brief spike (`pihub.device_alerts`)."""

from __future__ import annotations

from typing import List, Optional, Tuple

import pytest

from pihub.device_alerts import DEFAULT_RULES, AlertRule, DeviceAlerts

TEMP = AlertRule("cpu-temperature", 70.0, 80.0, 5.0, "CPU temperature is high", "CPU temperature is critical", "CPU temperature is back to normal", " °C")
CPU = AlertRule("cpu-usage", 90.0, 98.0, 10.0, "CPU usage has stayed high", "CPU usage has stayed very high", "CPU usage is back to normal", "%", sustained=3)


class Section:
    def __init__(self) -> None:
        self.sent: List[Tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.sent.append(("info", message))

    def warning(self, message: str) -> None:
        self.sent.append(("warning", message))

    def error(self, message: str) -> None:
        self.sent.append(("error", message))


class Reading:
    """A reading the test sets, then checks."""

    def __init__(self, rule: AlertRule = TEMP) -> None:
        self.value: Optional[float] = 40.0
        self.section = Section()
        self.alerts = DeviceAlerts(self.section, {rule.field: lambda: self.value}, [rule])

    def feed(self, *values: Optional[float]) -> List[Tuple[str, str]]:
        """Check once per value; return what was sent by these checks."""
        before = len(self.section.sent)
        for value in values:
            self.value = value
            self.alerts.check()
        return self.section.sent[before:]


def test_a_reading_that_passes_the_warning_level_sends_one_warning_and_then_nothing_while_it_stays_high():
    reading = Reading()
    assert reading.feed(60, 69.9) == []
    assert reading.feed(72.5) == [("warning", "CPU temperature is high: 72.5 °C")]
    assert reading.feed(73, 75, 79.9, 72) == []  # still the same episode


def test_passing_the_error_level_sends_an_error_even_from_a_warning():
    reading = Reading()
    assert reading.feed(72) == [("warning", "CPU temperature is high: 72 °C")]
    assert reading.feed(81) == [("error", "CPU temperature is critical: 81 °C")]
    assert reading.feed(85, 90) == []


def test_a_reading_that_jumps_straight_over_the_error_level_sends_only_the_error():
    assert Reading().feed(85) == [("error", "CPU temperature is critical: 85 °C")]


def test_easing_from_an_error_to_a_warning_is_quiet_and_recovery_sends_an_information_notice():
    reading = Reading()
    reading.feed(85)
    assert reading.feed(74) == []  # below the error level less its margin: still a warning, nothing new to say
    assert reading.feed(64.9) == [("info", "CPU temperature is back to normal: 64.9 °C")]


def test_a_reading_hovering_around_a_level_does_not_flap():
    reading = Reading()
    assert len(reading.feed(70.1)) == 1
    # It dips below 70 but not below 65 (the margin), and comes back: still the one episode.
    assert reading.feed(69, 68, 70.5, 66, 71, 65.5, 70.2) == []
    assert reading.feed(64.9) == [("info", "CPU temperature is back to normal: 64.9 °C")]
    assert len(reading.feed(70)) == 1  # a new episode


def test_an_error_level_reading_needs_its_own_margin_to_clear():
    reading = Reading()
    reading.feed(81)
    assert reading.feed(76, 79, 80.4) == []  # 76 is inside the margin (75 to 80): still critical, nothing new
    assert reading.feed(74) == []  # now a warning (quiet)
    assert reading.feed(81) == [("error", "CPU temperature is critical: 81 °C")]  # and it can escalate again


def test_a_brief_spike_is_not_an_alert_when_the_rule_is_sustained():
    reading = Reading(CPU)
    assert reading.feed(95, 95, 40, 95, 95, 40) == []  # never three in a row
    assert reading.feed(95, 95, 95) == [("warning", "CPU usage has stayed high: 95%")]


def test_sustained_counts_toward_the_error_level_too_and_lowering_is_immediate():
    reading = Reading(CPU)
    reading.feed(95, 95, 95)
    assert reading.feed(99, 99) == []
    assert reading.feed(99) == [("error", "CPU usage has stayed very high: 99%")]
    assert reading.feed(50) == [("info", "CPU usage is back to normal: 50%")]  # no waiting to say it is over


def test_a_missing_reading_changes_nothing_and_a_failing_reader_never_raises():
    reading = Reading()
    reading.feed(75)
    assert reading.feed(None, None) == []  # no reading is not "recovered"
    assert reading.feed(74) == []

    def broken() -> float:
        raise OSError("no such file")

    section = Section()
    DeviceAlerts(section, {"cpu-temperature": broken}, [TEMP]).check()
    assert section.sent == []


def test_a_rule_for_a_reading_the_machine_does_not_have_is_skipped():
    section = Section()
    alerts = DeviceAlerts(section, {}, DEFAULT_RULES)
    alerts.check()
    assert section.sent == []


def test_a_notification_that_cannot_be_sent_does_not_raise():
    class Offline(Section):
        def warning(self, message: str) -> None:
            raise RuntimeError("not connected")

    reading = Reading()
    reading.section = Offline()
    reading.alerts = DeviceAlerts(reading.section, {"cpu-temperature": lambda: reading.value}, [TEMP])
    reading.feed(75)  # must not raise; the episode counts as announced


def test_each_field_has_its_own_state():
    section = Section()
    values = {"cpu-temperature": 75.0, "disk-used": 96.0}
    disk = AlertRule("disk-used", 85.0, 95.0, 3.0, "Disk is getting full", "Disk is nearly full", "Disk space is back to normal", "%")
    alerts = DeviceAlerts(section, {key: (lambda k=key: values[k]) for key in values}, [TEMP, disk])
    alerts.check()
    alerts.check()
    assert section.sent == [("warning", "CPU temperature is high: 75 °C"), ("error", "Disk is nearly full: 96%")]


@pytest.mark.parametrize(
    "kwargs",
    [dict(warn_at=80.0, error_at=70.0), dict(warn_at=70.0, error_at=70.0), dict(clear_margin=-1.0), dict(sustained=0)],
)
def test_a_rule_that_makes_no_sense_is_refused(kwargs):
    values = dict(field="x", warn_at=70.0, error_at=80.0, clear_margin=5.0, high="h", critical="c", recovered="r")
    values.update(kwargs)
    with pytest.raises(ValueError):
        AlertRule(**values)


def test_the_default_rules_cover_temperature_cpu_memory_and_disk_with_sensible_levels():
    assert {rule.field for rule in DEFAULT_RULES} == {"cpu-temperature", "cpu-usage", "memory-used", "disk-used"}
    for rule in DEFAULT_RULES:
        assert rule.warn_at < rule.error_at and rule.clear_margin < rule.warn_at
    assert next(rule for rule in DEFAULT_RULES if rule.field == "cpu-usage").sustained > 1  # a spike is not an alert
