"""The built-in "Device Information" program (`pihub.device_information`).

A real device gets it automatically, first in its manifest. Each field exists only if its source exists on the machine; the live
readings are taken only while someone is viewing; it can be turned off; and it never reads or shows the serial number or machine id.
The readings are fed from fake `/proc`, `/sys` and command trees, so every branch runs without a Raspberry Pi.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional

import pytest

from pihub import Device, DeviceInformation, Program
from pihub.device_information import (
    DEVICE_INFORMATION_PROGRAM_ID,
    FIELDS,
    CpuMeter,
    Readings,
    Sources,
)

# -- a fake machine ------------------------------------------------------------------------------------------------------

STAT_1 = "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0 0 0\n"  # busy 200 of 1000
STAT_2 = "cpu  200 0 200 1500 0 0 0 0 0 0\n"  # busy 400 of 1900: +200 busy in +900 total = 22.2 %
WIRELESS = (
    "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
    " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
    " wlan0: 0000   60.  -50.  -256        0      0      0      0      0        0\n"
)


def make_machine(
    tmp_path: Path,
    *,
    pi: bool = True,
    wifi: bool = True,
    thermal: bool = True,
    throttled: Optional[str] = "throttled=0x0\n",
) -> Sources:
    """A fake root with only the files a given kind of machine would have. `throttled=None` means no `vcgencmd`."""
    proc, sys_, etc = tmp_path / "proc", tmp_path / "sys", tmp_path / "etc"
    for d in (proc / "net", proc / "device-tree", sys_ / "class" / "thermal" / "thermal_zone0", etc):
        d.mkdir(parents=True, exist_ok=True)
    (proc / "uptime").write_text("9000.55 8000.00\n")
    (proc / "stat").write_text(STAT_1)
    (proc / "meminfo").write_text("MemTotal:        1000000 kB\nMemFree: 100000 kB\nMemAvailable:    400000 kB\n")
    (etc / "os-release").write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nID=debian\n')
    if pi:
        (proc / "device-tree" / "model").write_bytes(b"Raspberry Pi 3 Model A Plus Rev 1.0\x00")
    if wifi:
        (proc / "net" / "wireless").write_text(WIRELESS)
    if thermal:
        (sys_ / "class" / "thermal" / "thermal_zone0" / "temp").write_text("47250\n")

    def run(command: List[str]) -> Optional[str]:
        return throttled if command[:2] == ["vcgencmd", "get_throttled"] else None

    return Sources(proc=proc, sys=sys_, etc=etc, root=str(tmp_path), run=run, hostname=lambda: "example-pi", machine=lambda: "armv6l")


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def runtime(self, widget: str) -> list:
        """Every value sent for one widget of the Device Information program, in order."""
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            binding = payload.get("binding", {})
            if binding.get("programId") == DEVICE_INFORMATION_PROGRAM_ID and binding.get("widgetId") == widget:
                found.append(payload.get("value"))
        return found

    def notifications(self) -> list:
        found = []
        for raw in list(self.sent):
            payload = json.loads(raw).get("payload", {})
            if "severity" in payload and payload.get("programId") == DEVICE_INFORMATION_PROGRAM_ID:
                found.append((payload["severity"], payload["message"]))
        return found

    def manifest_program_ids(self) -> List[str]:
        manifest = json.loads(self.sent[0])["payload"]
        return [p["programId"] for p in manifest["programs"]]

    def manifest_widgets(self, program_id: str) -> dict:
        manifest = json.loads(self.sent[0])["payload"]
        program = next(p for p in manifest["programs"] if p["programId"] == program_id)
        return {w["widgetId"]: w for w in program["widgets"]}


def started(sources: Sources, *, setting=True, programs=(), **options):
    """A started device with Device Information on, fed from `sources`. Returns (device, transport)."""
    transport = RecordingTransport()
    info = DeviceInformation(sources=sources, sample_seconds=0.02, **options) if setting is True else setting
    device = Device(id="example-pi-1", transport=transport, device_information=info)
    for program in programs:
        device.add_program(program)
    device.start()
    return device, transport


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


# -- the readings ----------------------------------------------------------------------------------------------------------


def test_the_pi_model_has_its_terminating_null_stripped_and_is_absent_on_a_machine_without_a_device_tree(tmp_path):
    assert Readings(make_machine(tmp_path)).pi_model() == "Raspberry Pi 3 Model A Plus Rev 1.0"
    assert Readings(make_machine(tmp_path / "other", pi=False)).pi_model() is None


def test_the_os_comes_from_os_release_with_the_architecture_and_falls_back_when_there_is_no_file(tmp_path):
    sources = make_machine(tmp_path)
    assert Readings(sources).operating_system() == "Debian GNU/Linux 13 (trixie) (armv6l)"
    (sources.etc / "os-release").unlink()
    assert Readings(sources).operating_system()  # something, from platform.platform()


@pytest.mark.parametrize(
    "text, expected",
    [("9000.55 1.0", "2 h 30 min"), ("100000 1", "1 d 3 h 46 min"), ("59 1", "0 h 0 min"), ("", None), ("garbage", None)],
)
def test_uptime_is_days_hours_minutes_and_absent_when_unreadable(tmp_path, text, expected):
    sources = make_machine(tmp_path)
    (sources.proc / "uptime").write_text(text)
    assert Readings(sources).uptime() == expected


def test_the_cpu_share_is_the_busy_change_over_the_total_change_and_needs_two_readings(tmp_path):
    sources = make_machine(tmp_path)
    meter = CpuMeter(Readings(sources).cpu_times)
    assert meter.percent() is None  # no baseline yet
    (sources.proc / "stat").write_text(STAT_2)
    assert meter.percent() == pytest.approx(22.2, abs=0.05)
    assert meter.percent() is None  # nothing has moved since: no interval, no number, and no divide by zero


def test_the_cpu_baseline_is_reset_when_a_viewer_arrives_so_an_idle_gap_is_not_averaged_in(tmp_path):
    sources = make_machine(tmp_path)
    meter = CpuMeter(Readings(sources).cpu_times)
    meter.percent()  # baseline at STAT_1
    (sources.proc / "stat").write_text(STAT_2)  # a long idle gap passes...
    meter.reset()  # ...a viewer arrives: the baseline is now STAT_2
    (sources.proc / "stat").write_text("cpu  300 0 300 1600 0 0 0 0 0 0\n")  # +200 busy of +300 total since the reset
    assert meter.percent() == pytest.approx(66.7, abs=0.05)  # (without the reset it would be 33.3: diluted by the idle gap)


def test_the_temperature_is_millidegrees_and_absent_without_a_thermal_zone(tmp_path):
    assert Readings(make_machine(tmp_path)).cpu_temperature() == 47.2 or Readings(make_machine(tmp_path)).cpu_temperature() == 47.3
    assert Readings(make_machine(tmp_path / "x", thermal=False)).cpu_temperature() is None


def test_temperature_garbage_is_absent_not_an_error(tmp_path):
    sources = make_machine(tmp_path)
    (sources.sys / "class" / "thermal" / "thermal_zone0" / "temp").write_text("hot\n")
    assert Readings(sources).cpu_temperature() is None


def test_memory_used_is_one_minus_available_over_total_and_absent_if_a_field_is_missing(tmp_path):
    sources = make_machine(tmp_path)
    assert Readings(sources).memory_used_percent() == 60.0
    (sources.proc / "meminfo").write_text("MemTotal: 1000 kB\n")
    assert Readings(sources).memory_used_percent() is None


def test_disk_used_is_a_percentage_and_absent_for_a_path_that_does_not_exist(tmp_path):
    sources = make_machine(tmp_path)
    assert 0.0 <= Readings(sources).disk_used_percent() <= 100.0
    sources.root = str(tmp_path / "nope")
    assert Readings(sources).disk_used_percent() is None


@pytest.mark.parametrize(
    "output, expected",
    [
        ("throttled=0x0\n", ("OK", "")),
        ("throttled=0x50000\n", ("OK", "")),  # under-voltage HAS occurred since boot, but not now: not a warning
        ("throttled=0x1\n", ("Warning", "under-voltage (check the power supply and cable)")),
        ("throttled=0x5\n", ("Warning", "under-voltage (check the power supply and cable); the CPU is throttled")),
    ],
)
def test_power_reads_the_current_flags_of_get_throttled(tmp_path, output, expected):
    assert Readings(make_machine(tmp_path, throttled=output)).power() == expected


@pytest.mark.parametrize("output", [None, "", "no equals sign", "throttled=zzz\n"])
def test_power_is_absent_when_there_is_no_vcgencmd_or_its_output_is_not_understood(tmp_path, output):
    assert Readings(make_machine(tmp_path, throttled=output)).power() is None


def test_wifi_signal_is_the_first_interfaces_level_and_absent_without_wifi_or_with_an_unknown_level(tmp_path):
    sources = make_machine(tmp_path)
    assert Readings(sources).wifi_signal_dbm() == -50.0
    (sources.proc / "net" / "wireless").write_text(WIRELESS.replace("-50.", "-256."))  # the driver says "unknown"
    assert Readings(sources).wifi_signal_dbm() is None
    assert Readings(make_machine(tmp_path / "x", wifi=False)).wifi_signal_dbm() is None
    (sources.proc / "net" / "wireless").write_text("nonsense\n")
    assert Readings(sources).wifi_signal_dbm() is None


def test_the_ip_address_is_absent_when_the_machine_has_no_route(monkeypatch, tmp_path):
    import socket

    class NoNetwork:
        def __init__(self, *a, **k):
            raise OSError("no route")

    monkeypatch.setattr(socket, "socket", NoNetwork)
    assert Readings(make_machine(tmp_path)).ip_address() is None


# -- which fields exist on which machine -----------------------------------------------------------------------------------


def field_ids(sources: Sources, **kwargs) -> set:
    _, transport = started(sources, **kwargs)
    return set(transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID))


def test_a_pi_gets_every_default_field(tmp_path):
    assert field_ids(make_machine(tmp_path)) == {
        "device-id", "pi-model", "hostname", "operating-system", "sdk-version", "uptime", "cpu-usage",
        "cpu-temperature", "memory-used", "disk-used", "power", "wifi-signal",
    }


def test_a_plain_server_simply_gets_fewer_fields_and_nothing_is_registered_for_a_source_it_lacks(tmp_path):
    bare = make_machine(tmp_path, pi=False, wifi=False, thermal=False, throttled=None)
    ids = field_ids(bare)
    assert {"pi-model", "power", "wifi-signal", "cpu-temperature", "camera-sensor", "ip-address"}.isdisjoint(ids)
    assert {"device-id", "hostname", "operating-system", "uptime", "cpu-usage", "memory-used", "disk-used"} <= ids


def test_the_ip_address_is_optional_and_off_unless_asked_for(tmp_path, monkeypatch):
    import socket

    class FakeSocket:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def connect(self, address): ...
        def getsockname(self): return ("192.168.1.50", 12345)

    monkeypatch.setattr(socket, "socket", FakeSocket)
    sources = make_machine(tmp_path)
    assert "ip-address" not in field_ids(sources)
    assert "ip-address" in field_ids(make_machine(tmp_path / "b"), show_ip_address=True)


def test_the_serial_number_and_machine_id_are_never_fields():
    ids = {f.id for f in FIELDS}
    assert not any("serial" in i or "machine" in i for i in ids)


def test_every_widget_has_a_declared_title_and_every_gauge_declares_its_units(tmp_path):
    _, transport = started(make_machine(tmp_path))
    widgets = transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID)
    for widget_id, widget in widgets.items():
        defaults = widget["defaultProperties"]
        assert defaults["title"] and defaults["title"] != widget_id, widget_id  # "CPU usage", not "cpu-usage"
        if widget["type"] == "Gauge":
            assert defaults["units"] is not None, widget_id
    assert widgets["cpu-usage"]["defaultProperties"] == {"title": "CPU usage", "units": "%"}
    assert widgets["cpu-temperature"]["type"] == "Gauge" and widgets["hostname"]["type"] == "Status"


# -- being part of a device -------------------------------------------------------------------------------------------------


def test_a_device_that_was_given_its_own_transport_is_left_exactly_as_it_was(tmp_path):
    transport = RecordingTransport()
    device = Device(id="sim", transport=transport)  # a simulation or test: no device_information given
    device.add_program(Program(id="mine", name="Mine"))
    device.start()
    assert transport.manifest_program_ids() == ["mine"]


def test_it_can_be_forced_off_and_forced_on(tmp_path):
    for setting, expected in ((False, ["mine"]), (True, [DEVICE_INFORMATION_PROGRAM_ID, "mine"])):
        transport = RecordingTransport()
        device = Device(id="d", transport=transport, device_information=setting)
        device.add_program(Program(id="mine", name="Mine"))
        device.start()
        assert transport.manifest_program_ids() == expected


def test_it_is_first_in_the_manifest_however_many_programs_the_device_added(tmp_path):
    _, transport = started(make_machine(tmp_path), programs=[Program(id="a", name="A"), Program(id="b", name="B")])
    assert transport.manifest_program_ids() == [DEVICE_INFORMATION_PROGRAM_ID, "a", "b"]


def test_a_program_of_the_devices_own_with_that_id_is_respected_and_not_duplicated(tmp_path):
    _, transport = started(make_machine(tmp_path), programs=[Program(id=DEVICE_INFORMATION_PROGRAM_ID, name="Mine")])
    assert transport.manifest_program_ids() == [DEVICE_INFORMATION_PROGRAM_ID]
    assert transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID) == {}


def test_the_static_fields_and_a_first_reading_are_published_once_the_device_has_started(tmp_path):
    _, transport = started(make_machine(tmp_path))
    assert transport.runtime("device-id") == ["example-pi-1"]
    assert transport.runtime("pi-model") == ["Raspberry Pi 3 Model A Plus Rev 1.0"]
    assert transport.runtime("hostname") == ["example-pi"]
    assert transport.runtime("operating-system") == ["Debian GNU/Linux 13 (trixie) (armv6l)"]
    assert transport.runtime("sdk-version") and isinstance(transport.runtime("sdk-version")[0], str)
    assert transport.runtime("memory-used") == [60.0]
    assert transport.runtime("power") == ["OK"]
    assert transport.runtime("wifi-signal") == [-50.0]
    assert transport.runtime("cpu-usage") == []  # a CPU share needs two readings: no baseline yet


# -- sampling only while someone is looking ---------------------------------------------------------------------------------


def program_of(device) -> "object":
    return device._programs[DEVICE_INFORMATION_PROGRAM_ID]


def test_nothing_is_sampled_until_a_viewer_arrives_and_it_stops_when_the_last_one_leaves(tmp_path):
    device, transport = started(make_machine(tmp_path))
    program = program_of(device)
    time.sleep(0.15)
    assert not program.sampling and len(transport.runtime("uptime")) == 1  # only the first reading, no timer

    device.simulate_lifecycle("firstUserConnected")
    assert program.sampling
    assert wait_for(lambda: len(transport.runtime("uptime")) >= 4)

    device.simulate_lifecycle("lastUserDisconnected")
    assert not program.sampling
    time.sleep(0.1)  # let any in-flight sample land
    settled = len(transport.runtime("uptime"))
    time.sleep(0.15)
    assert len(transport.runtime("uptime")) == settled  # and no more after that


def test_a_viewer_count_above_zero_starts_sampling_as_a_safety_net_and_zero_stops_it(tmp_path):
    device, _ = started(make_machine(tmp_path))
    program = program_of(device)
    device.simulate_lifecycle("viewerCountChanged", viewerCount=2)
    assert program.sampling
    device.simulate_lifecycle("viewerCountChanged", viewerCount=0)
    assert not program.sampling
    device.simulate_lifecycle("viewerCountChanged", viewerCount="not a number")  # ignored, not an error
    assert not program.sampling


def test_a_second_first_viewer_event_does_not_start_a_second_sampler(tmp_path):
    device, _ = started(make_machine(tmp_path))
    before = threading.active_count()
    device.simulate_lifecycle("firstUserConnected")
    device.simulate_lifecycle("firstUserConnected")
    assert threading.active_count() == before + 1
    device.simulate_lifecycle("lastUserDisconnected")


def test_stopping_the_device_stops_the_sampler_and_stopping_twice_is_fine(tmp_path):
    device, _ = started(make_machine(tmp_path))
    device.simulate_lifecycle("firstUserConnected")
    program = program_of(device)
    assert program.sampling
    device.stop()
    assert not program.sampling
    device.stop()


def test_the_first_live_number_after_a_viewer_arrives_covers_only_that_visit(tmp_path):
    sources = make_machine(tmp_path)
    device, transport = started(sources)
    (sources.proc / "stat").write_text(STAT_2)  # the machine got busy while nobody was looking
    device.simulate_lifecycle("firstUserConnected")  # the baseline is taken NOW, at STAT_2
    time.sleep(0.15)
    assert transport.runtime("cpu-usage") == []  # the static file has not moved since: no interval, no number
    (sources.proc / "stat").write_text("cpu  300 0 300 1600 0 0 0 0 0 0\n")
    assert wait_for(lambda: transport.runtime("cpu-usage") != [])
    assert transport.runtime("cpu-usage")[0] == pytest.approx(66.7, abs=0.1)  # not 33.3 % diluted by the idle gap
    device.simulate_lifecycle("lastUserDisconnected")


# -- power warnings ---------------------------------------------------------------------------------------------------------


def test_a_bad_power_reading_warns_once_each_time_it_turns_bad_not_on_every_sample(tmp_path):
    answers = iter(["throttled=0x0\n", "throttled=0x1\n", "throttled=0x1\n", "throttled=0x0\n", "throttled=0x1\n"])
    sources = make_machine(tmp_path)
    # the first answer is used by the probe at start, the rest by the samples below
    sources.run = lambda command: next(answers)
    device, transport = started(sources)  # probe: OK; first reading at start: (next) Warning
    program = program_of(device)
    for _ in range(3):
        program._publish_live()
    warnings = transport.notifications()
    assert [severity for severity, _ in warnings] == ["warning", "warning"]  # 0x1 (start), then 0x1 again after an OK
    assert "under-voltage" in warnings[0][1] and "power supply" in warnings[0][1]
    assert transport.runtime("power") == ["Warning", "Warning", "OK", "Warning"]  # the status always shown; the WARNING only on a change


def test_the_power_status_shown_is_only_ok_or_warning(tmp_path):
    _, transport = started(make_machine(tmp_path, throttled="throttled=0x5\n"))
    assert set(transport.runtime("power")) <= {"OK", "Warning"}
    assert transport.runtime("power")[0] == "Warning"


# -- robustness -------------------------------------------------------------------------------------------------------------


def test_a_reading_that_raises_is_skipped_and_the_others_are_still_published(tmp_path):
    device, transport = started(make_machine(tmp_path))
    program = program_of(device)
    program.widget("memory-used").read = lambda: 1 / 0
    before = len(transport.runtime("uptime"))
    program._publish_live()
    assert len(transport.runtime("uptime")) == before + 1


def test_a_connection_that_cannot_send_right_now_does_not_break_sampling(tmp_path):
    device, transport = started(make_machine(tmp_path))
    program = program_of(device)

    def failing(data):
        raise OSError("not connected")

    transport.send = failing
    program._publish_live()  # must not raise; the next sample or the reconnect's re-send covers it


def test_a_program_hook_that_raises_does_not_stop_the_device_starting(tmp_path):
    class Bad(Program):
        def _device_started(self):
            raise RuntimeError("boom")

        def _device_stopped(self):
            raise RuntimeError("boom")

    device, _ = started(make_machine(tmp_path), programs=[Bad(id="bad", name="Bad")])
    device.stop()  # and stopping is fine too


# -- the camera sensor ------------------------------------------------------------------------------------------------------


class FakeCameraSource:
    real_camera = True

    def start(self): ...
    def stop(self): ...


def camera_program() -> Program:
    program = Program(id="birdbox", name="Bird Box")
    program.output(id="camera", type="Video").use(FakeCameraSource())
    return program


def test_the_camera_sensor_is_a_field_only_when_the_device_has_a_real_camera(tmp_path):
    assert "camera-sensor" not in field_ids(make_machine(tmp_path))
    assert "camera-sensor" in field_ids(make_machine(tmp_path / "b"), programs=[camera_program()])


def test_the_camera_sensor_is_in_the_camera_group_and_the_other_fields_are_not_grouped(tmp_path):
    _, transport = started(make_machine(tmp_path), programs=[camera_program()])
    widgets = transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID)

    assert widgets["camera-sensor"]["defaultProperties"]["group"] == "Camera"
    assert all("group" not in w.get("defaultProperties", {}) for id, w in widgets.items() if id != "camera-sensor" and not id.startswith("camera-"))


def test_a_warning_from_device_information_is_sent_as_device_informations(tmp_path):
    device, transport = started(make_machine(tmp_path))
    device.information.warning("The camera's picture is strongly pink")

    assert ("warning", "The camera's picture is strongly pink") in transport.notifications()  # `notifications()` keeps only Device Information's


# -- alerts from the vitals, whether or not anyone is viewing --------------------------------------------------------------------


def hot_machine(tmp_path: Path, celsius: str = "74500") -> Sources:
    machine = make_machine(tmp_path)
    (machine.sys / "class" / "thermal" / "thermal_zone0" / "temp").write_text(celsius + "\n")
    return machine


def test_a_hot_cpu_sends_a_warning_from_device_information_with_nobody_viewing(tmp_path):
    machine = hot_machine(tmp_path)
    _, transport = started(machine, setting=DeviceInformation(sources=machine, sample_seconds=0.02, alert_seconds=0.02))  # no viewer ever connects

    warning = ("warning", "CPU temperature is high: 74.5 °C")
    assert wait_for(lambda: warning in transport.notifications())
    time.sleep(0.15)
    assert transport.notifications().count(warning) == 1  # once, not at every check


def test_a_cool_machine_sends_no_alert(tmp_path):
    machine = make_machine(tmp_path)
    _, transport = started(machine, setting=DeviceInformation(sources=machine, sample_seconds=0.02, alert_seconds=0.02))
    time.sleep(0.3)
    assert [n for n in transport.notifications() if n[0] in ("warning", "error")] == []


def test_alerts_can_be_turned_off(tmp_path):
    machine = hot_machine(tmp_path)
    _, transport = started(machine, setting=DeviceInformation(sources=machine, sample_seconds=0.02, alert_seconds=0.02, alerts=False))
    time.sleep(0.3)
    assert transport.notifications() == []


def test_a_device_can_use_its_own_alert_levels(tmp_path):
    from pihub.device_alerts import AlertRule

    rule = AlertRule("cpu-temperature", 40.0, 90.0, 2.0, "Too warm for a bird box", "Far too warm", "Cool again", " C")
    machine = make_machine(tmp_path)  # 47.25 C
    _, transport = started(machine, setting=DeviceInformation(sources=machine, sample_seconds=0.02, alert_seconds=0.02, alerts=[rule]))
    assert wait_for(lambda: any(kind == "warning" and text.startswith("Too warm for a bird box: 47.") for kind, text in transport.notifications()))


def test_the_first_check_is_one_interval_after_start_so_a_booting_device_is_quiet(tmp_path):
    machine = hot_machine(tmp_path)
    _, transport = started(machine, setting=DeviceInformation(sources=machine, alert_seconds=30))
    time.sleep(0.2)
    assert transport.notifications() == []


@pytest.mark.parametrize("detected, shown", [("imx219", "imx219"), (None, "Not detected")])
def test_the_camera_sensor_is_read_on_a_thread_after_start_and_says_so_when_none_is_found(tmp_path, monkeypatch, detected, shown):
    import pihub.camera_setup as camera_setup

    monkeypatch.setattr(camera_setup, "detect_sensor", lambda: detected)
    _, transport = started(make_machine(tmp_path), programs=[camera_program()])
    assert wait_for(lambda: transport.runtime("camera-sensor") == [shown])


def test_a_test_pattern_video_source_does_not_count_as_a_camera(tmp_path):
    from pihub.video import TestPatternSource

    program = Program(id="demo", name="Demo")
    program.output(id="camera", type="Video").use(TestPatternSource())
    assert "camera-sensor" not in field_ids(make_machine(tmp_path), programs=[program])


def test_the_option_is_exported_from_the_package():
    import pihub

    assert pihub.DeviceInformation is DeviceInformation


# -- the section a device's own code can reach ------------------------------------------------------------------------------------


def unstarted(sources, **options):
    transport = RecordingTransport()
    info = DeviceInformation(sources=sources, sample_seconds=0.02, **options)
    return Device(id="example-pi-1", transport=transport, device_information=info), transport


def test_a_device_can_reach_the_section_before_it_starts_with_the_standard_fields_already_in_it(tmp_path):
    device, transport = unstarted(make_machine(tmp_path))
    info = device.information
    assert info.id == DEVICE_INFORMATION_PROGRAM_ID and info.widget("hostname") is not None and info.widget("cpu-usage") is not None
    device.start()
    assert transport.manifest_program_ids() == [DEVICE_INFORMATION_PROGRAM_ID]


def test_a_standard_field_can_be_taken_out_before_start(tmp_path):
    device, transport = unstarted(make_machine(tmp_path))
    device.information.remove("wifi-signal")
    device.information.remove("power")
    device.start()
    widgets = set(transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID))
    assert "wifi-signal" not in widgets and "power" not in widgets and "hostname" in widgets
    assert transport.runtime("wifi-signal") == [] and transport.runtime("power") == []  # and nothing was read for them


def test_a_device_can_add_its_own_field_with_the_same_call_it_uses_for_any_section(tmp_path):
    device, transport = unstarted(make_machine(tmp_path))
    info = device.information
    info.output(id="battery", type="Gauge", read=lambda: 87.5, refresh="live", properties=[])
    info.output(id="site", type="Status").set  # a plain output is pushed by hand, as anywhere
    device.start()
    assert "battery" in transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID)
    assert transport.runtime("battery") == [87.5]  # a first reading at start, like the standard live fields
    device.simulate_lifecycle("firstUserConnected")
    assert wait_for(lambda: len(transport.runtime("battery")) >= 3)
    device.simulate_lifecycle("lastUserDisconnected")


def test_asking_for_the_section_turns_it_on_even_for_a_device_given_its_own_transport(tmp_path):
    transport = RecordingTransport()
    device = Device(id="sim", transport=transport, device_information=DeviceInformation(sources=make_machine(tmp_path)))
    device.add_program(Program(id="mine", name="Mine"))
    device.information  # noqa: B018 - asking is the point
    device.start()
    assert transport.manifest_program_ids() == [DEVICE_INFORMATION_PROGRAM_ID, "mine"]


def test_the_accessor_after_start_is_refused_when_the_section_was_never_built(tmp_path):
    transport = RecordingTransport()
    device = Device(id="sim", transport=transport)  # a simulation: no Device Information
    device.start()
    with pytest.raises(RuntimeError, match="already started"):
        device.information  # noqa: B018


def test_a_removed_field_is_not_brought_back_by_what_a_camera_adds(tmp_path, monkeypatch):
    import pihub.camera_setup as camera_setup

    monkeypatch.setattr(camera_setup, "detect_sensor", lambda: "imx219")
    device, transport = unstarted(make_machine(tmp_path))
    device.add_program(camera_program())
    device.information.remove("camera-sensor")  # removed BEFORE the camera's fields exist: it is remembered
    device.start()
    assert "camera-sensor" not in transport.manifest_widgets(DEVICE_INFORMATION_PROGRAM_ID)
