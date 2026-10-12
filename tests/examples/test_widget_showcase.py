"""The Widget Showcase (examples/widget_showcase.py): every widget the platform can draw, driven the way a real dashboard drives it.

Read this file top to bottom as a tour of what the platform can do. Each test says what the *dashboard* would do (a person presses a button,
drags a slider, opens the page) and shows what the *device* does in answer. No server is needed: `transport` is a recording stand-in for the
connection, and `device.handle_incoming(...)` is where the dashboard's messages arrive.

KEEP THIS UP TO DATE. When a widget type is added to the dashboard's widget registry, it must be added to
`examples/widget_showcase.py` and to this file: `test_the_showcase_uses_every_widget_the_dashboard_can_draw` fails until it is.
"""

import re
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("av")  # the Camera widget needs PyAV for their test pattern

from pihub import Device  # noqa: E402
from pihub.protocol.bindings import Binding  # noqa: E402
from pihub.protocol.envelope import Envelope  # noqa: E402
from pihub.protocol.messages import CommandMessage, LifecycleMessage  # noqa: E402
from examples import widget_showcase  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CYCLES = 8  # enough readings for the temperature to climb past the warning line and come back down again


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # The Camera's default snapshot and recording folders are under the home directory.
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def shown(transport) -> Device:
    """The showcase after a short run: it registered, started, and published CYCLES rounds of readings."""
    return widget_showcase.main(transport=transport, cycles=CYCLES, interval=0)


# -- helpers: reading what the device sent -------------------------------------------------------------------------------------------


def payloads(transport, message_type: str) -> List[Dict[str, Any]]:
    """Everything of one kind the device has sent so far: "registration", "runtime" (values), "notification"."""
    return [Envelope.from_json(raw).payload for raw in transport.sent if Envelope.from_json(raw).message_type == message_type]


def registered_widgets(transport) -> Dict[str, Dict[str, Any]]:
    """What the device told the platform it has, in its first message (the registration): {"program.widget": {"type": ..., "properties": ...}}."""
    [registration] = payloads(transport, "registration")
    return {
        f"{program['programId']}.{widget['widgetId']}": {"type": widget["type"], "properties": widget.get("defaultProperties", {})}
        for program in registration["programs"]
        for widget in program["widgets"]
    }


def published(transport, program: str, widget: str) -> List[Any]:
    """Every single value the device has published for one widget, oldest first."""
    return [
        p["value"]
        for p in payloads(transport, "runtime")
        if p["binding"] == {"deviceId": "example-device", "programId": program, "widgetId": widget} and "value" in p
    ]


def user_does(device: Device, program: str, widget: str, **message: Any) -> None:
    """The dashboard sends the device a command: `action="press"` for a button, `values={"value": ...}` to change a setting."""
    binding = Binding(device_id="example-device", program_id=program, widget_id=widget)
    device.handle_incoming(CommandMessage(binding=binding, **message).to_json())


def messages(transport, severity: str) -> List[str]:
    return [n["message"] for n in payloads(transport, "notification") if n["severity"] == severity]


# -- 1. The showcase runs, and says what it has -------------------------------------------------------------------------------------


def test_it_runs_and_returns_the_device(transport):
    device = widget_showcase.main(transport=transport, cycles=1, interval=0)
    assert isinstance(device, Device)
    assert device.id == "example-device"


def test_a_program_is_a_section_of_the_dashboard_and_this_one_makes_four(shown, transport):
    # Each `Program` shows on the dashboard as a section with its own heading (the dashboard adds the heading from the program's name).
    [registration] = payloads(transport, "registration")
    names = {program["programId"]: program["name"] for program in registration["programs"]}
    assert names["readouts"] == "Showcase: Readouts"
    assert names["controls"] == "Showcase: Controls"
    assert names["camera"] == "Showcase: Camera"
    assert names["files"] == "Showcase: Files"


def test_the_showcase_declares_every_kind_of_widget(shown, transport):
    types = {widget: info["type"] for widget, info in registered_widgets(transport).items() if not widget.startswith("device-information.")}
    assert types == {
        # Readouts: the device pushes values, the dashboard only shows them.
        "readouts.temperature": "Gauge",
        "readouts.humidity": "Gauge",
        "readouts.pressure": "Gauge",
        "readouts.sensor-state": "Status",
        "readouts.conditions": "Chart",
        # Controls: the dashboard sends things back.
        "controls.lights-on": "Button",
        "controls.lights-off": "Button",
        "controls.lights-blink": "Button",
        "controls.door-open": "Button",
        "controls.door-close": "Button",
        "controls.door-lock": "Button",
        "controls.armed": "Toggle",
        "controls.mode": "Select",
        "controls.brightness": "Slider",
        "controls.greeting": "Text",
        # Camera: a live picture, straight from the device to the browser, with its own buttons.
        "camera.camera": "Camera",
        # Files: the Camera's own folders, downloaded straight to the browser, peer to peer.
        "files.files": "Files",
    }


# -- 2. The guard: a new widget type cannot be added without a place in the showcase ------------------------------------------------

# Widget types the dashboard can draw that a program does not declare, and why. Add to this only with a reason.
NOT_DECLARED_BY_A_PROGRAM = {
    "Heading": "the dashboard adds one to every program from the program's name; a program never declares it",
    "Video": "the Camera widget is the standard way to show a camera, and it draws the same picture with its own buttons. A device streams "
    "one camera at a time and every video widget for it plays that stream, so a plain Video beside a Camera would only play in step with it",
}


# The public copy of the SDK has no dashboard source to read, so the guards that read its widget registry skip there; in the full repository a registry
# that cannot be found or parsed is a failure.
needs_the_frontend = pytest.mark.skipif(not (REPO / "frontend").is_dir(), reason="no dashboard source in this copy of the repository")


def _widget_types_the_dashboard_can_draw() -> List[str]:
    """The keys of WIDGET_COMPONENTS in the frontend: the types the page can turn into something on the screen."""
    source = (REPO / "frontend" / "src" / "widgets" / "registry.ts").read_text()
    body = re.search(r"WIDGET_COMPONENTS[^=]*=\s*\{(.*?)\n\}", source, re.S)
    assert body, "could not find WIDGET_COMPONENTS in the frontend widget registry: update this test to match"
    return re.findall(r"^\s+(\w+):", body.group(1), re.M)


@needs_the_frontend
def test_the_showcase_uses_every_widget_the_dashboard_can_draw(shown, transport):
    drawable = _widget_types_the_dashboard_can_draw()
    assert len(drawable) >= 10, f"read too few widget types from the frontend registry: {drawable}"

    used = {info["type"] for info in registered_widgets(transport).values()}
    missing = [t for t in drawable if t not in used and t not in NOT_DECLARED_BY_A_PROGRAM]

    assert not missing, (
        f"the dashboard can draw {missing}, but examples/widget_showcase.py does not use it. Add it to the showcase (with a comment saying what it is "
        f"for), then to this file: a test for how it behaves, and its line in test_the_showcase_declares_every_kind_of_widget."
    )


@needs_the_frontend
def test_every_exemption_is_still_a_widget_the_dashboard_can_draw():
    # An exemption for a type that no longer exists is clutter that hides the next real omission: remove it.
    assert set(NOT_DECLARED_BY_A_PROGRAM) <= set(_widget_types_the_dashboard_can_draw())


@needs_the_frontend
def test_the_showcase_only_uses_widgets_the_dashboard_can_draw(shown, transport):
    # The other direction: a typo in a widget type would register fine and then silently draw nothing.
    used = {info["type"] for info in registered_widgets(transport).values()}
    assert used <= set(_widget_types_the_dashboard_can_draw())


# -- 3. Readouts: Gauge, Status and Chart --------------------------------------------------------------------------------------------


def test_a_gauge_shows_one_number_with_its_units(shown, transport):
    widgets = registered_widgets(transport)
    assert widgets["readouts.temperature"]["properties"] == {"title": "Temperature", "units": "°C"}
    temperatures = published(transport, "readouts", "temperature")
    assert len(temperatures) == CYCLES  # one number per cycle
    assert all(isinstance(t, float) for t in temperatures)


def test_a_chart_is_fed_several_values_at_once(shown, transport):
    # A Chart is fed with `set_values`: one message carrying all the series. (A Gauge is fed one number with `set`.) The dashboard draws
    # each value as its own panel and keeps the history; the platform itself only ever holds the latest update.
    updates = [
        p for p in payloads(transport, "runtime") if p["binding"]["widgetId"] == "conditions" and "values" in p
    ]
    assert len(updates) == CYCLES
    assert set(updates[0]["values"]) == {"temperature", "humidity", "pressure"}


def test_a_status_is_a_word_and_the_dashboard_colours_it(shown, transport):
    # "Online" is green and "Warning" amber; the dashboard decides the colour from the word.
    states = published(transport, "readouts", "sensor-state")
    assert states[0] == "Online"
    assert "Warning" in states  # the temperature climbs past the line during the run
    assert states[-1] == "Online"  # ...and comes back down


def test_a_warning_notification_is_sent_once_when_the_line_is_crossed_not_on_every_reading(shown, transport):
    # `program.warning(...)` appears on the dashboard and stays until someone acknowledges it. Four readings were too hot, but that is one warning.
    assert messages(transport, "warning") == ["Temperature is high"]
    assert messages(transport, "info")[0] == "Showcase started"


# -- 4. Buttons: the dashboard runs your code ----------------------------------------------------------------------------------------


def test_two_groups_of_buttons_are_two_rows(shown, transport):
    # `group="Lights"` is a hint the dashboard uses: widgets with the same group sit together under a small heading, and a run of buttons in
    # one group is one row. A second group is a second row, whatever kind of widget follows or precedes it.
    widgets = registered_widgets(transport)
    groups = {}
    for name, info in widgets.items():
        if info["type"] == "Button":
            groups.setdefault(info["properties"]["group"], []).append(info["properties"]["title"])
    assert groups == {"Lights": ["On", "Off", "Blink"], "Doors": ["Open", "Close", "Lock"]}


@pytest.mark.parametrize(
    "widget, notice",
    [("lights-on", "Lights: On"), ("lights-blink", "Lights: Blink"), ("door-lock", "Doors: Lock")],
)
def test_pressing_a_button_runs_the_devices_code(shown, transport, capsys, widget, notice):
    # A person presses a button: the dashboard sends a command with `action="press"`, and the function under `@handler` runs on the device.
    user_does(shown, "controls", widget, action="press")

    assert notice in messages(transport, "info")  # the handler told the dashboard (its Notification history)
    assert "pressed" in capsys.readouterr().out  # and printed in the terminal the program runs in


def test_a_press_for_one_button_does_not_run_another(shown, transport):
    user_does(shown, "controls", "door-open", action="press")
    assert [m for m in messages(transport, "info") if m.startswith(("Lights", "Doors"))] == ["Doors: Open"]


# -- 5. Settings: Toggle, Select, Slider and Text -------------------------------------------------------------------------------------


def test_settings_start_at_their_defaults_and_publish_them(shown, transport):
    # A setting shows the value in force, so the device publishes it as soon as it starts.
    assert published(transport, "controls", "armed") == [False]
    assert published(transport, "controls", "mode") == ["Auto"]
    assert published(transport, "controls", "brightness") == [50]
    assert published(transport, "controls", "greeting") == ["Hello!"]


def test_a_toggle_is_switched_from_the_dashboard(shown, transport, capsys):
    user_does(shown, "controls", "armed", values={"value": True})

    assert published(transport, "controls", "armed") == [False, True]  # published back, so every open dashboard agrees
    assert "Armed is now True" in capsys.readouterr().out  # `apply` ran: this is where your code acts on the change


def test_a_select_takes_one_of_its_options(shown, transport, capsys):
    user_does(shown, "controls", "mode", values={"value": "Night"})

    assert published(transport, "controls", "mode") == ["Auto", "Night"]
    assert "Mode is now Night" in capsys.readouterr().out


def test_a_slider_takes_a_number_in_range_and_snaps_to_its_step(shown, transport):
    # minimum 0, maximum 100, step 5: 47 is not a step, so it becomes 45.
    user_does(shown, "controls", "brightness", values={"value": 47})

    assert published(transport, "controls", "brightness") == [50, 45]


def test_a_text_setting_is_submitted_from_the_dashboard(shown, transport, capsys):
    user_does(shown, "controls", "greeting", values={"value": "Hi there"})

    assert published(transport, "controls", "greeting") == ["Hello!", "Hi there"]
    assert "Greeting is now 'Hi there'" in capsys.readouterr().out


@pytest.mark.parametrize(
    "widget, bad",
    [
        ("armed", "yes"),
        ("mode", "Purple"),
        ("brightness", 250),
        ("brightness", "bright"),
        ("greeting", 123),
        ("greeting", "x" * 501),  # over max_length=500
    ],
)
def test_the_device_refuses_a_setting_that_is_not_valid_and_warns(shown, transport, widget, bad):
    # The device, not the browser, has the last word: nothing changes, nothing is published, and the dashboard is told why.
    before = published(transport, "controls", widget)

    user_does(shown, "controls", widget, values={"value": bad})

    assert published(transport, "controls", widget) == before
    assert any(m.startswith("Setting not changed") for m in messages(transport, "warning"))


def test_a_setting_survives_a_restart(shown, transport):
    # Settings are saved on the device (`persist=True` is the default), so a program that is stopped and started again comes back as it was.
    user_does(shown, "controls", "brightness", values={"value": 80})

    class Again:
        def __init__(self) -> None:
            self.sent: List[str] = []

        def send(self, data: str) -> None:
            self.sent.append(data)

    again = Again()
    widget_showcase.main(transport=again, cycles=0, interval=0)

    assert published(again, "controls", "brightness") == [80]


# -- 6. Camera -------------------------------------------------------------------------------------------------------------


def test_the_camera_offers_snapshot_and_record(shown, transport):
    widgets = registered_widgets(transport)
    assert widgets["camera.camera"]["type"] == "Camera"
    # The Camera draws its own buttons; these say which ones this device offers.
    assert widgets["camera.camera"]["properties"] == {"snapshot": True, "record": True}


def test_the_video_stream_is_announced_but_nothing_streams_until_someone_watches(shown, transport):
    # The camera publishes where its stream is, and then does nothing: frames flow only once a browser asks, peer to peer.
    [camera_state] = [p["value"] for p in payloads(transport, "runtime") if p["binding"]["widgetId"] == "camera"]
    assert "streamId" in camera_state


def test_the_device_hears_when_viewers_arrive_and_leave(shown, transport):
    # `on_first_user_connected` and `on_last_user_disconnected` are how a program saves work while nobody is looking.
    shown.handle_incoming(LifecycleMessage.first_user_connected().to_json())
    shown.handle_incoming(LifecycleMessage.last_user_disconnected().to_json())

    assert messages(transport, "info")[-2:] == ["Viewer connected", "Last viewer disconnected"]


# -- 7. Files ----------------------------------------------------------------------------------------------------------------


def test_files_lists_the_cameras_own_folders(shown, transport):
    # Nothing has been RECORDED in this short run, and the recordings folder is fully redirected under the fixture's tmp_path (it is
    # computed fresh each time `camera.recorder()` runs), so it starts empty. The snapshot folder's *default* is a plain dataclass field
    # on `Camera`, evaluated once when the class is first imported - not per instance - so it stays wherever this process's real $HOME
    # pointed at import time, not this test's tmp_path; the showcase does not override it (a tinkerer copying it would not either), so
    # this only checks that both folders are there, not that Photos is empty.
    [state] = published(transport, "files", "files")
    assert {folder["label"] for folder in state["folders"]} == {"Videos", "Photos"}
    videos = next(folder for folder in state["folders"] if folder["label"] == "Videos")
    assert videos["files"] == []


def test_files_lists_what_is_actually_on_disk_when_asked(shown, transport):
    videos_directory = shown._programs["files"]._widgets["files"].folder("Videos")
    videos_directory.mkdir(parents=True, exist_ok=True)
    (videos_directory / "rec-1-00001-20260101-120000.mp4").write_bytes(b"not a real recording")

    user_does(shown, "files", "files", action="list")

    listed = published(transport, "files", "files")[-1]
    videos = next(folder for folder in listed["folders"] if folder["label"] == "Videos")
    assert [f["name"] for f in videos["files"]] == ["rec-1-00001-20260101-120000.mp4"]


def test_deleting_a_file_removes_it_from_disk_and_the_list(shown, transport):
    videos_directory = shown._programs["files"]._widgets["files"].folder("Videos")
    videos_directory.mkdir(parents=True, exist_ok=True)
    target = videos_directory / "rec-1-00001-20260101-120000.mp4"
    target.write_bytes(b"not a real recording")
    user_does(shown, "files", "files", action="list")

    user_does(shown, "files", "files", values={"delete": {"folder": "Videos", "name": target.name}})

    assert not target.exists()
    listed = published(transport, "files", "files")[-1]
    videos = next(folder for folder in listed["folders"] if folder["label"] == "Videos")
    assert videos["files"] == []


def test_a_finished_recording_republishes_the_list_by_itself(shown, transport):
    # `recorder.on_saved(...)` wires this up in the showcase - a program never has to poll its own folders. The
    # listener ignores its argument (it only needs the folder to have changed, not what changed in it), so a real
    # `Recording` is not needed to prove the wiring.
    before = len(published(transport, "files", "files"))
    recorder = shown._programs["camera"]._widgets["camera"]._recorder
    videos_directory = shown._programs["files"]._widgets["files"].folder("Videos")
    videos_directory.mkdir(parents=True, exist_ok=True)
    fake = videos_directory / "rec-1-00001-20260101-120000.mp4"
    fake.write_bytes(b"not a real recording")
    for listener in recorder._saved_listeners:
        listener(None)

    after = published(transport, "files", "files")
    assert len(after) == before + 1
    videos = next(folder for folder in after[-1]["folders"] if folder["label"] == "Videos")
    assert [f["name"] for f in videos["files"]] == [fake.name]

