"""Widget Showcase - every widget ThePiHub can draw, in one program you can copy from.

Copy this file anywhere and run it:

    python widget_showcase.py

If this is the first time you have connected from this device, it prints a link and a code: open the link
while logged in to ThePiHub and approve it, then answer y when the terminal asks you to confirm the account.
The device is yours and gets its id. Every run after that connects straight away. Ctrl+C stops it.

What to look for on the dashboard. Each `Program` becomes a section with a heading, and this one makes three:

  Readouts   Gauge (one number), Status (a coloured word), Chart (several numbers over time).
  Controls   Button (runs your code), Toggle, Select, Slider and Text (a setting the dashboard changes and the device remembers).
  Camera     Camera (a live picture with snapshot, record and full-screen buttons), as in the Bird Box example.
  Files      Files (the Camera's Videos and Photos folders; download one, or "Download all" straight to your machine).

`group=` puts widgets under a small heading inside their section: the two sets of buttons below are two groups, so they are two rows.
Widgets of the same kind sit together on their own: gauges as tiles, buttons in a row, toggles/selects/sliders as slim rows, a chart or a
video on a line of its own. You never lay anything out; you say what each widget is and the dashboard does the rest.

The readings are made-up numbers standing in for sensors, so the gauges and the chart move: replace `_read_conditions()`.
The picture is a moving test pattern (a bar sweeping across a gradient); on a Raspberry Pi with a camera, it is the real camera.
The Camera needs the video pieces (the plain `pip install thepihub` brings PyAV; on a 32-bit Pi there is no pip build of PyAV, so install it with apt (`sudo apt install python3-picamera2 python3-av python3-aiortc`) and create the virtualenv with `--system-site-packages`). Files needs the same extra (a download is peer to peer, over its own DataChannel) and, like the Camera's video, only works over a direct connection.

Adding a widget type to the platform? Add it here too: tests/examples/test_widget_showcase.py fails until you do.
"""

from __future__ import annotations

import math
import time
from typing import Dict, Optional

from pihub import Device, Program, StringProperty
from pihub.device import Transport
from pihub.protocol.messages import CommandMessage
from pihub.video import EncodedTestPatternSource, HardwareCameraSource

# A temperature above this turns the Status to "Warning" and sends a warning notification (once, when it crosses).
TOO_HOT = 28.0


def _read_conditions(step: int) -> Dict[str, float]:
    """Made-up readings that drift smoothly, standing in for real sensor hardware."""
    return {
        "temperature": round(22.0 + 8.0 * math.sin(step / 3), 1),
        "humidity": round(60.0 + 25.0 * math.sin(step / 5), 1),
        "pressure": round(1015.0 + 20.0 * math.sin(step / 7), 1),
    }


def main(transport: Optional[Transport] = None, cycles: Optional[int] = None, interval: float = 2.0) -> Device:
    """`transport` and `cycles` are for the tests: left out, it connects to the real server and runs until Ctrl+C."""
    device = Device(transport=transport)

    # ---- Section 1: Readouts. Outputs: the device pushes values, the dashboard only shows them. -------------------------------
    readouts = Program(id="readouts", name="Showcase: Readouts")

    # Gauge: one number. `units` is shown after it. `title` is the label (without one, the id is used).
    temperature = readouts.output(
        id="temperature", type="Gauge", properties=[StringProperty("title", default="Temperature"), StringProperty("units", default="°C")]
    )
    humidity = readouts.output(
        id="humidity", type="Gauge", properties=[StringProperty("title", default="Humidity"), StringProperty("units", default="%")]
    )
    pressure = readouts.output(
        id="pressure", type="Gauge", properties=[StringProperty("title", default="Pressure"), StringProperty("units", default="hPa")]
    )

    # Status: a word in a coloured pill. "Online" and "Running" are green, "Warning" amber, "Offline", "Stopped" and "Fault" red,
    # anything else grey.
    sensor_state = readouts.output(id="sensor-state", type="Status", properties=[StringProperty("title", default="Sensor")])

    # Chart: several values published together, drawn over time as one small panel each. Use `set_values` (not `set`) for it.
    conditions = readouts.output(id="conditions", type="Chart", properties=[StringProperty("title", default="Conditions")])

    # ---- Section 2: Controls. Buttons run your code; settings hold a value the dashboard can change. ------------------------------
    controls = Program(id="controls", name="Showcase: Controls")

    # Button: `command()` makes one, and `@handler` is the code that runs when it is pressed. `group=` puts buttons under a heading:
    # a different group is a different row.
    def button(id: str, title: str, group: str):
        widget = controls.command(id=id, type="Button", properties=[StringProperty("title", default=title)], group=group)

        @widget.handler
        def pressed(command: CommandMessage) -> None:
            # Your code goes here (switch a relay, open a valve). Then tell the dashboard, which keeps a Notification history.
            print(f"{title} pressed")  # shows in this terminal
            controls.info(f"{group}: {title}")  # and in the dashboard's Notification history

        return widget

    button("lights-on", "On", group="Lights")
    button("lights-off", "Off", group="Lights")
    button("lights-blink", "Blink", group="Lights")
    button("door-open", "Open", group="Doors")
    button("door-close", "Close", group="Doors")
    button("door-lock", "Lock", group="Doors")

    # Toggle, Select and Slider are *settings*: they show the value in force, the dashboard changes it, and the device checks it, saves it (so it
    # survives a restart), publishes it back and then calls `apply` with it. A value out of range is refused with a warning.
    # `.value` is what is set now; `.set(...)` changes it from your own code.
    controls.setting(
        "armed", "Toggle", default=False, title="Armed", group="Settings",
        apply=lambda value: print(f"Armed is now {value}"),
    )
    controls.setting(
        "mode", "Select", default="Auto", options=["Auto", "Day", "Night"], title="Mode", group="Settings",
        apply=lambda value: print(f"Mode is now {value}"),
    )
    controls.setting(
        "brightness", "Slider", default=50, minimum=0, maximum=100, step=5, units="%", title="Brightness", group="Settings",
        apply=lambda value: print(f"Brightness is now {value}"),
    )
    # Text: a free-text setting with an explicit Submit on the dashboard, not sent on every keystroke - same
    # check/save/publish/apply pipeline as Toggle/Select/Slider, just for a string. `max_length=` is optional.
    controls.setting(
        "greeting", "Text", default="Hello!", max_length=500, title="Greeting", group="Settings",
        apply=lambda value: print(f"Greeting is now {value!r}"),
    )

    # ---- Section 3: Camera. Peer to peer: the picture goes straight from the device to the browser, and only while someone watches. ----
    camera_section = Program(id="camera", name="Showcase: Camera")

    # Camera: a live picture with buttons for a snapshot, recording and full screen. `use()` says where the frames come from: the Pi's own
    # camera if there is one, otherwise a moving test pattern (a bright bar sweeping across a gradient) so you can see it is live.
    camera = camera_section.output(id="camera", type="Camera")
    camera.use(HardwareCameraSource() if HardwareCameraSource.available() else EncodedTestPatternSource())
    recorder = camera.recorder()  # adds the Record button (recordings and snapshots are saved on the device)

    # The camera has nothing to do while nobody is watching: these say when someone starts or stops. A program can also react to the viewer count.
    @camera_section.on_first_user_connected
    def viewer_arrived(payload: dict) -> None:
        camera_section.info("Viewer connected")

    @camera_section.on_last_user_disconnected
    def viewers_left(payload: dict) -> None:
        camera_section.info("Last viewer disconnected")

    # ---- Section 4: Files. The Camera's own folders - "Download all" saves everything in one straight to your machine, peer to peer,
    # deleting each one off the device once it has arrived (the widget's own toggle, on the dashboard, turns that off). ----------------
    files_section = Program(id="files", name="Showcase: Files")
    files = files_section.output(id="files", type="Files")
    files.add_folder("Videos", recorder.store.directory)
    files.add_folder("Photos", camera.snapshot_directory)
    recorder.on_saved(lambda _recording: files.publish())  # a finished recording shows up without a page reload
    # A snapshot has no equivalent hook yet: press the widget's own Refresh (it just asks the device to list its folders again).

    for program in (readouts, controls, camera_section, files_section):
        device.add_program(program)
    device.start()

    # ---- Notifications: `info`, `warning` and `error` appear on the dashboard; a warning stays until someone acknowledges it. ----------
    readouts.info("Showcase started")

    hot = False
    done = 0
    try:
        while cycles is None or done < cycles:
            readings = _read_conditions(done)

            # One value per Gauge.
            temperature.set(readings["temperature"])
            humidity.set(readings["humidity"])
            pressure.set(readings["pressure"])

            # Several values at once, for the Chart.
            conditions.set_values(readings)

            # A Status is just a word, and a warning is raised once when the temperature crosses the line, not on every reading.
            too_hot = readings["temperature"] > TOO_HOT
            sensor_state.set("Warning" if too_hot else "Online")
            if too_hot and not hot:
                readouts.warning("Temperature is high")
            hot = too_hot

            done += 1
            if interval:
                time.sleep(interval)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        device.stop()

    return device


if __name__ == "__main__":
    main()  # pragma: no cover
