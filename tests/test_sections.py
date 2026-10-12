"""The section features every section gets: pull-style outputs (`read=` / `refresh=`), settable components (`setting`), `remove`, and `Section`.

Device Information is built from these same calls (see `test_device_information.py`); this file tests them on an ordinary section.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable, List

import pytest

from pihub import Device, DeviceInformation, Program, Section
from pihub.properties import NumberProperty
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import CommandMessage
from pihub.settings_store import SettingsStore


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: List[str] = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def payloads(self) -> List[dict]:
        return [json.loads(raw).get("payload", {}) for raw in list(self.sent)]

    def values(self, program: str, widget: str) -> list:
        out = []
        for payload in self.payloads():
            binding = payload.get("binding", {})
            if binding.get("programId") == program and binding.get("widgetId") == widget:
                out.append(payload.get("value"))
        return out

    def notifications(self) -> list:
        return [(p["severity"], p["message"]) for p in self.payloads() if "severity" in p]

    def manifest(self) -> dict:
        return json.loads(self.sent[0])["payload"]


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def started(*sections: Program):
    transport = RecordingTransport()
    device = Device(id="dev", transport=transport, device_information=False)
    for section in sections:
        device.add_program(section)
    device.start()
    return device, transport


def command_for(program: str, widget: str, **kwargs) -> str:
    return CommandMessage(binding=Binding(device_id="dev", program_id=program, widget_id=widget), **kwargs).to_json()


# -- the name ------------------------------------------------------------------------------------------------------------------


def test_section_is_the_same_class_as_program_and_the_wire_still_says_program():
    assert Section is Program
    device, transport = started(Section("garden", "Garden"))
    assert transport.manifest()["programs"][0]["programId"] == "garden"


def test_a_device_makes_a_section_or_returns_the_one_it_has():
    device = Device(id="dev", transport=RecordingTransport(), device_information=False)
    garden = device.section("garden", "Garden")
    assert isinstance(garden, Section) and device.section("garden", "ignored") is garden


# -- pull-style outputs --------------------------------------------------------------------------------------------------------


def test_a_static_reader_is_read_once_after_the_device_starts():
    calls = []
    section = Section("s", "S")
    section.output(id="model", type="Status", read=lambda: calls.append(1) or "Zero W")
    assert calls == []  # not before the device has started: the section is not attached to anything yet
    _, transport = started(section)
    assert calls == [1] and transport.values("s", "model") == ["Zero W"]


def test_a_reader_that_returns_none_publishes_nothing_and_one_that_raises_is_skipped():
    section = Section("s", "S")
    section.output(id="absent", type="Status", read=lambda: None)
    section.output(id="broken", type="Status", read=lambda: 1 / 0)
    section.output(id="fine", type="Status", read=lambda: "yes")
    _, transport = started(section)
    assert transport.values("s", "absent") == [] and transport.values("s", "broken") == [] and transport.values("s", "fine") == ["yes"]


def test_a_slow_static_reader_can_run_on_a_thread_so_start_is_not_held_up():
    release = threading.Event()
    section = Section("s", "S")
    section.output(id="slow", type="Status", read=lambda: release.wait(5) and "done", background=True)
    _, transport = started(section)  # returns at once even though the reader is blocked
    assert transport.values("s", "slow") == []
    release.set()
    assert wait_for(lambda: transport.values("s", "slow") == ["done"])


def test_live_readings_are_taken_only_while_a_viewer_is_present():
    section = Section("s", "S")
    section.sample_seconds = 0.02
    ticks = []
    section.output(id="n", type="Gauge", read=lambda: ticks.append(1) or len(ticks), refresh="live")
    device, transport = started(section)
    time.sleep(0.12)
    assert not section.sampling and len(transport.values("s", "n")) == 1  # the one first reading at start, then nothing
    device.simulate_lifecycle("firstUserConnected")
    assert section.sampling and wait_for(lambda: len(transport.values("s", "n")) >= 4)
    device.simulate_lifecycle("lastUserDisconnected")
    assert not section.sampling
    time.sleep(0.08)
    settled = len(transport.values("s", "n"))
    time.sleep(0.12)
    assert len(transport.values("s", "n")) == settled


def test_each_output_can_have_its_own_interval():
    section = Section("s", "S")
    section.sample_seconds = 10.0  # the default is far too slow to see in a test...
    section.output(id="fast", type="Gauge", read=lambda: 1, refresh="live", every=0.02)
    section.output(id="slow", type="Gauge", read=lambda: 1, refresh="live", every=10.0)  # ...and this one is slower still
    device, transport = started(section)
    device.simulate_lifecycle("firstUserConnected")
    assert wait_for(lambda: len(transport.values("s", "fast")) >= 5)
    assert len(transport.values("s", "slow")) == 2  # the start reading and the first sampling tick, then not due for 10 s
    device.simulate_lifecycle("lastUserDisconnected")


def test_a_reader_with_a_reset_is_reset_each_time_sampling_starts():
    class Meter:
        resets = 0

        def reset(self):
            Meter.resets += 1

        def __call__(self):
            return 1.0

    section = Section("s", "S")
    section.sample_seconds = 0.02
    section.output(id="cpu", type="Gauge", read=Meter(), refresh="live")
    device, _ = started(section)
    device.simulate_lifecycle("firstUserConnected")
    device.simulate_lifecycle("lastUserDisconnected")
    device.simulate_lifecycle("firstUserConnected")
    device.simulate_lifecycle("lastUserDisconnected")
    assert Meter.resets == 2


def test_an_always_reader_runs_without_a_viewer_and_stops_when_the_device_stops():
    section = Section("s", "S")
    section.output(id="tick", type="Gauge", read=lambda: 1, refresh="always", every=0.02)
    device, transport = started(section)
    assert wait_for(lambda: len(transport.values("s", "tick")) >= 4)  # nobody is viewing
    device.stop()
    time.sleep(0.08)
    settled = len(transport.values("s", "tick"))
    time.sleep(0.12)
    assert len(transport.values("s", "tick")) == settled


def test_the_reader_arguments_are_checked():
    section = Section("s", "S")
    with pytest.raises(ValueError, match="only apply to an output that has a `read`"):
        section.output(id="a", type="Gauge", refresh="live")
    with pytest.raises(ValueError, match="refresh must be one of"):
        section.output(id="b", type="Gauge", read=lambda: 1, refresh="sometimes")
    with pytest.raises(ValueError, match="every must be positive"):
        section.output(id="c", type="Gauge", read=lambda: 1, refresh="live", every=0)


def test_an_ordinary_output_with_no_reader_behaves_exactly_as_it_did():
    section = Section("s", "S")
    temperature = section.output(id="temperature", type="Gauge")
    assert temperature.read is None and temperature.refresh is None
    device, transport = started(section)
    temperature.set(21.5)
    assert transport.values("s", "temperature") == [21.5] and not section.sampling


# -- remove --------------------------------------------------------------------------------------------------------------------


def test_a_component_can_be_removed_before_start_and_is_then_not_in_the_manifest():
    section = Section("s", "S")
    section.output(id="keep", type="Gauge")
    section.output(id="drop", type="Gauge")
    section.remove("drop")
    _, transport = started(section)
    assert [w["widgetId"] for w in transport.manifest()["programs"][0]["widgets"]] == ["keep"]


def test_removing_after_start_is_refused_because_the_manifest_is_fixed():
    section = Section("s", "S")
    section.output(id="x", type="Gauge")
    started(section)
    with pytest.raises(RuntimeError, match="already started"):
        section.remove("x")


def test_removing_an_id_that_is_not_there_yet_is_remembered_so_the_sdk_leaves_it_out_later():
    section = Section("s", "S")
    section.remove("camera-bitrate")
    assert section.is_excluded("camera-bitrate")
    section.output(id="camera-bitrate", type="Gauge")  # adding it yourself puts it back
    assert not section.is_excluded("camera-bitrate")


# -- settings ------------------------------------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "settings.json")


def make(section_id="cam", store=None):
    section = Section(section_id, "Camera")
    section.settings_store = store
    return section


def test_a_toggle_select_and_slider_register_with_display_and_command_and_carry_their_choices_in_the_schema(store):
    section = make(store=store)
    section.setting("noir", "Toggle", default=False, title="Camera is NoIR")
    section.setting("size", "Select", default="640x480", options=["320x240", "640x480", "1280x720"], title="Resolution")
    section.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1, title="Frame rate", units="fps")
    _, transport = started(section)
    widgets = {w["widgetId"]: w for w in transport.manifest()["programs"][0]["widgets"]}
    assert set(widgets) == {"noir", "size", "fps"}
    for widget in widgets.values():
        assert widget["capabilities"] == ["display", "command"]
    def schema(widget, name):
        return next(p for p in widgets[widget]["propertySchema"] if p["name"] == name)
    assert widgets["noir"]["type"] == "Toggle" and widgets["noir"]["defaultProperties"] == {"title": "Camera is NoIR", "value": False}
    assert schema("size", "value")["options"] == ["320x240", "640x480", "1280x720"] and widgets["size"]["defaultProperties"]["value"] == "640x480"
    assert {k: schema("fps", "value")[k] for k in ("min", "max", "step")} == {"min": 1, "max": 30, "step": 1}
    assert widgets["fps"]["defaultProperties"] == {"title": "Frame rate", "units": "fps", "value": 15}


def test_a_text_setting_registers_with_display_and_command_and_carries_its_max_length_in_the_schema(store):
    section = make(store=store)
    section.setting("greeting", "Text", default="hi", max_length=10, title="Greeting")
    _, transport = started(section)
    widget = next(w for w in transport.manifest()["programs"][0]["widgets"] if w["widgetId"] == "greeting")
    assert widget["type"] == "Text" and widget["capabilities"] == ["display", "command"]
    value_schema = next(p for p in widget["propertySchema"] if p["name"] == "value")
    assert value_schema["maxLength"] == 10
    assert widget["defaultProperties"] == {"title": "Greeting", "value": "hi"}


def test_a_text_setting_with_no_max_length_has_none_in_its_schema(store):
    section = make(store=store)
    section.setting("greeting", "Text", default="hi")
    _, transport = started(section)
    widget = next(w for w in transport.manifest()["programs"][0]["widgets"] if w["widgetId"] == "greeting")
    value_schema = next(p for p in widget["propertySchema"] if p["name"] == "value")
    assert "maxLength" not in value_schema


def test_the_dashboard_can_change_a_text_setting_and_it_is_saved_published_and_applied(store):
    applied = []
    section = make(store=store)
    section.setting("greeting", "Text", default="hi", apply=applied.append)
    device, transport = started(section)
    device.handle_incoming(command_for("cam", "greeting", values={"value": "hello there"}))
    assert section.widget("greeting").value == "hello there"
    assert store.get("cam.greeting") == "hello there"
    assert transport.values("cam", "greeting") == ["hi", "hello there"]  # the start value, then the change
    assert applied == ["hello there"]


def test_a_setting_publishes_its_current_value_when_the_device_starts(store):
    section = make(store=store)
    section.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1)
    _, transport = started(section)
    assert transport.values("cam", "fps") == [15]


def test_the_dashboard_can_change_a_setting_and_it_is_saved_published_and_applied(store):
    applied = []
    section = make(store=store)
    section.setting("size", "Select", default="640x480", options=["640x480", "1280x720"], apply=applied.append)
    device, transport = started(section)
    device.handle_incoming(command_for("cam", "size", values={"value": "1280x720"}))
    assert section.widget("size").value == "1280x720"
    assert store.get("cam.size") == "1280x720"  # saved, keyed by section and widget
    assert transport.values("cam", "size") == ["640x480", "1280x720"]  # the start value, then the change
    assert applied == ["1280x720"]


def test_a_saved_value_wins_over_the_default_when_the_device_starts_again(store):
    store.set("cam.fps", 24)
    section = make(store=store)
    section.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1)
    _, transport = started(section)
    assert section.widget("fps").value == 24 and transport.values("cam", "fps") == [24]


def test_a_saved_value_that_is_no_longer_valid_is_ignored_in_favour_of_the_default(store):
    store.set("cam.size", "9999x9999")  # a resolution that is no longer offered
    section = make(store=store)
    section.setting("size", "Select", default="640x480", options=["640x480", "1280x720"])
    assert section.widget("size").value == "640x480"


@pytest.mark.parametrize(
    "type_, kwargs, bad",
    [
        ("Toggle", {"default": False}, "yes"),
        ("Toggle", {"default": False}, 1),
        ("Select", {"default": "a", "options": ["a", "b"]}, "c"),
        ("Slider", {"default": 5, "minimum": 1, "maximum": 10, "step": 1}, 11),
        ("Slider", {"default": 5, "minimum": 1, "maximum": 10, "step": 1}, 0),
        ("Slider", {"default": 5, "minimum": 1, "maximum": 10, "step": 1}, True),
        ("Slider", {"default": 5, "minimum": 1, "maximum": 10, "step": 1}, "7"),
        ("Text", {"default": ""}, 123),
        ("Text", {"default": ""}, True),
        ("Text", {"default": "", "max_length": 5}, "this is too long"),
    ],
)
def test_the_device_checks_every_change_and_ignores_one_the_dashboard_should_not_have_sent(store, type_, kwargs, bad):
    section = make(store=store)
    setting = section.setting("s", type_, **kwargs)
    device, transport = started(section)
    before = setting.value
    device.handle_incoming(command_for("cam", "s", values={"value": bad}))
    assert setting.value == before and store.get("cam.s") is None  # unchanged and not saved
    assert transport.notifications() and transport.notifications()[-1][0] == "warning"


def test_a_slider_snaps_to_its_step_and_stays_a_whole_number_when_everything_is_whole(store):
    section = make(store=store)
    slider = section.setting("kbit", "Slider", default=1000, minimum=100, maximum=8000, step=100)
    started(section)
    assert slider.set(1049) == 1000 and isinstance(slider.value, int)
    assert slider.set(1051) == 1100


def test_a_command_for_another_section_with_the_same_widget_id_is_ignored(store):
    a, b = make("a", store), make("b", store)
    a.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1)
    b.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1)
    device, _ = started(a, b)
    device.handle_incoming(command_for("b", "fps", values={"value": 30}))  # routing is by widget id alone, so BOTH handlers hear it
    assert a.widget("fps").value == 15 and b.widget("fps").value == 30


def test_a_command_with_an_action_and_no_value_is_left_alone(store):
    section = make(store=store)
    setting = section.setting("noir", "Toggle", default=False)
    device, transport = started(section)
    device.handle_incoming(command_for("cam", "noir", action="press"))
    assert setting.value is False and transport.values("cam", "noir") == [False]


def test_a_removed_setting_no_longer_answers_commands(store):
    section = make(store=store)
    setting = section.setting("noir", "Toggle", default=False)
    section.remove("noir")
    device, _ = started(section)
    device.handle_incoming(command_for("cam", "noir", values={"value": True}))
    assert setting.value is False


def test_an_apply_that_raises_still_saves_the_value_and_says_so(store):
    def broken(value):
        raise RuntimeError("camera busy")

    section = make(store=store)
    section.setting("fps", "Slider", default=15, minimum=1, maximum=30, step=1, apply=broken)
    device, transport = started(section)
    device.handle_incoming(command_for("cam", "fps", values={"value": 20}))
    assert store.get("cam.fps") == 20
    assert "saved, but applying it failed" in transport.notifications()[-1][1]


def test_persist_false_keeps_a_setting_in_memory_only(tmp_path):
    section = make(store=SettingsStore(tmp_path / "settings.json"))
    setting = section.setting("noir", "Toggle", default=False, persist=False)
    started(section)
    setting.set(True)
    assert not (tmp_path / "settings.json").exists()


def test_a_setting_can_be_saved_somewhere_else(store):
    class Elsewhere:
        saved = {}

        def get(self, key, default=None):
            return self.saved.get(key, default)

        def set(self, key, value):
            self.saved[key] = value
            return True

    elsewhere = Elsewhere()
    section = make(store=store)
    setting = section.setting("noir", "Toggle", default=False, store=elsewhere)
    started(section)
    setting.set(True)
    assert elsewhere.saved == {"cam.noir": True} and store.get("cam.noir") is None


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.setting("x", "Text Input"),
        lambda s: s.setting("x", "Select", default="a", options=[]),
        lambda s: s.setting("x", "Slider", default=1),
        lambda s: s.setting("x", "Slider", default=50, minimum=1, maximum=10),
        lambda s: s.setting("x", "Select", default="z", options=["a"]),
        lambda s: s.setting("x", "Text", max_length=0),
        lambda s: s.setting("x", "Text", default=123),
    ],
)
def test_a_badly_declared_setting_is_a_programming_error_that_raises_at_once(call, store):
    with pytest.raises((ValueError, TypeError)):
        call(make(store=store))


# -- the store -----------------------------------------------------------------------------------------------------------------


def test_the_store_round_trips_keeps_other_keys_and_never_raises(tmp_path):
    store = SettingsStore(tmp_path / "sub" / "settings.json")
    assert store.get("a.b") is None and not store.has("a.b")
    assert store.set("a.b", 1) and store.set("c.d", "x")
    assert store.get("a.b") == 1 and store.get("c.d") == "x" and store.has("a.b")
    assert store.remove("a.b") and not store.has("a.b") and store.get("c.d") == "x"
    assert not store.remove("nope")


@pytest.mark.parametrize("content", ["", "not json", "[]", "3"])
def test_a_damaged_file_is_nothing_saved(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    store = SettingsStore(path)
    assert store.get("a", "fallback") == "fallback" and store.set("a", 1) and store.get("a") == 1


def test_an_unwritable_folder_makes_set_report_false_instead_of_raising(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert SettingsStore(blocker / "settings.json").set("a", 1) is False


def test_the_default_path_follows_xdg_config_home_beside_the_identity_file(tmp_path, monkeypatch):
    from pihub.device_config import default_config_path
    from pihub.settings_store import default_settings_path

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_settings_path() == tmp_path / "thepihub" / "settings.json"
    assert default_settings_path().parent == default_config_path().parent


# -- every: periodic work while the device runs -----------------------------------------------------------------------------------


def test_every_runs_a_task_repeatedly_while_the_device_runs_and_the_first_run_is_one_interval_in():
    calls: List[float] = []
    garden = Section("garden", "Garden")
    garden.every(0.05, lambda: calls.append(time.monotonic()))
    device, _ = started(garden)
    assert calls == []  # not at start: a device is still booting
    assert wait_for(lambda: len(calls) >= 3)
    device.stop()


def test_every_stops_when_the_device_stops():
    calls: List[int] = []
    garden = Section("garden", "Garden")
    garden.every(0.02, lambda: calls.append(1))
    device, _ = started(garden)
    assert wait_for(lambda: len(calls) >= 1)
    device.stop()
    time.sleep(0.1)
    settled = len(calls)
    time.sleep(0.15)
    assert len(calls) == settled


def test_a_task_that_raises_is_skipped_and_the_next_run_still_happens():
    calls: List[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    garden = Section("garden", "Garden")
    garden.every(0.02, flaky)
    device, _ = started(garden)
    assert wait_for(lambda: len(calls) >= 3)
    device.stop()


def test_every_needs_a_positive_interval_and_cannot_be_added_after_start():
    garden = Section("garden", "Garden")
    for bad in (0, -1):
        with pytest.raises(ValueError):
            garden.every(bad, lambda: None)
    started(garden)
    with pytest.raises(RuntimeError):
        garden.every(1, lambda: None)


# -- group: a hint for the dashboard -------------------------------------------------------------------------------------------


def registered(transport: RecordingTransport, program: str) -> dict:
    manifest = transport.manifest()
    return {w["widgetId"]: w for p in manifest["programs"] if p["programId"] == program for w in p["widgets"]}


def test_a_group_travels_as_an_ordinary_property_on_an_output_a_command_and_a_setting(tmp_path):
    garden = Section("garden", "Garden")
    garden.output("temperature", "Gauge", group="Weather")
    garden.command("water", "Button", group="Watering")
    garden.setting("led", "Toggle", persist=False, group="Weather")
    _, transport = started(garden)
    widgets = registered(transport, "garden")

    assert widgets["temperature"]["defaultProperties"]["group"] == "Weather"
    assert widgets["water"]["defaultProperties"]["group"] == "Watering"
    assert widgets["led"]["defaultProperties"]["group"] == "Weather"
    assert any(item["name"] == "group" for item in widgets["led"]["propertySchema"])


def test_a_component_with_no_group_is_registered_exactly_as_before():
    garden = Section("garden", "Garden")
    garden.output("temperature", "Gauge")
    garden.setting("led", "Toggle", persist=False)
    _, transport = started(garden)
    widgets = registered(transport, "garden")

    assert "group" not in widgets["temperature"].get("defaultProperties", {})
    assert "group" not in widgets["led"]["defaultProperties"]


def test_a_group_is_trimmed_and_must_be_a_non_empty_string():
    garden = Section("garden", "Garden")
    garden.output("a", "Gauge", group="  Weather ")
    assert next(p for p in garden.widget("a").properties if p.name == "group").default == "Weather"
    for bad in ("", "   ", 3):
        with pytest.raises(ValueError):
            garden.output(f"bad{bad}", "Gauge", group=bad)


def test_the_group_is_given_once():
    from pihub.properties import StringProperty

    with pytest.raises(ValueError):
        Section("garden", "Garden").output("a", "Gauge", [StringProperty(name="group", default="X")], group="Y")


# -- number property bounds ----------------------------------------------------------------------------------------------------


def test_number_property_bounds_appear_in_the_schema_only_when_set_and_are_checked():
    assert NumberProperty("v").to_dict() == {"name": "v", "label": "V", "control": "number"}  # unchanged for every existing property
    assert NumberProperty("v", default=5, minimum=1, maximum=10, step=1).to_dict()["max"] == 10
    for kwargs in ({"minimum": 5, "maximum": 1}, {"step": 0}, {"default": 99, "maximum": 10}, {"minimum": "1"}):
        with pytest.raises((ValueError, TypeError)):
            NumberProperty("v", **kwargs)
