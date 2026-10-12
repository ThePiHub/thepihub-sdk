# ThePiHub SDK

Put a Raspberry Pi (or any Linux machine) on a web dashboard without writing any web code. You say what the device has - a temperature to
show, a button that runs your code, a setting to change, a camera - and ThePiHub draws the dashboard, keeps it live, and works when you
are away from home. You never lay anything out.

```python
from pihub import Device, Program

device = Device()

weather = Program(id="weather", name="Weather Station")
temperature = weather.output(id="temperature", type="Gauge")
device.add_program(weather)
device.start()

temperature.set(21.5)     # the gauge on the dashboard now reads 21.5
```

The first time you run it at a terminal, it prints a link and a code to approve in your browser, and the platform gives the Pi its id; every run after
that connects straight away. There is nothing to add to your program for that. (A program with no terminal, such as a service started at boot, never
waits for anyone: `pihub install my_program.py` links the machine and installs the service in one step.)

This package is the **device** side only. It talks to ThePiHub, the hosted dashboard at <https://thepihub.com>, where you need an account.
It is alpha software (0.1): expect the API to move.

## Install

```bash
pip install thepihub
```

The package is called `thepihub`; you `import pihub`, and the command is `pihub`. Python 3.10 or newer, Linux (built for Raspberry Pi OS).

That one command installs everything pip can install. It includes the WebRTC library (aiortc and PyAV), so a browser can connect straight to your device and the
live **Camera** widget works. (`thepihub[webrtc]`, which older instructions use, still works and means the same.)

Two things come from somewhere else:

- **On a 32-bit Pi** (an original Pi Zero) pip has no ready-made PyAV, so the install leaves the WebRTC library out. Install it with apt instead, and make the virtualenv
  with `--system-site-packages`: `sudo apt install python3-picamera2 python3-av python3-aiortc`. If it is missing, the device says so in its log when it starts.
- **The Pi camera and the pin driver** are Raspberry Pi OS packages, not something pip can install: `sudo apt install python3-picamera2` for the camera.

One optional extra: `pip install "thepihub[gpio]"` adds gpiozero, for buttons, LEDs and sensors on the Pi's pins (see GPIO pins below).

## First run

The first time a machine connects, the program prints a link and a code. Open the link while logged in to ThePiHub, approve it, and
answer `y` in the terminal when it asks you to confirm the account. The device is then yours and the platform gives it its id; every
later run connects straight away. Nothing is typed onto the Pi but the `y`, so it works headless over SSH.

The saved identity lives in `~/.config/thepihub/config.json`. To set a machine up without running a program, use `pihub setup`.
The server is `https://thepihub.com` unless you pass `--server` or set `PIHUB_SERVER`.

## How it fits together

```text
Device            this machine, connected to the platform
  Program         a named group of things; it becomes one section of the dashboard
    output        a value you publish              -> Gauge, Status, Chart, Camera
    command       something the dashboard can run  -> Button
    setting       a value the dashboard can change -> Toggle, Select, Slider
```

The **device** says what it has. The **platform** owns the dashboards, the layout and how things look: you choose a widget by name and
give it a title; you never say where it goes. `group="Lights"` on any component is a hint that it belongs with others.

In the snippets below, `program` is a `Program` you create like the one at the top: declare its outputs, commands and settings, then
`device.add_program(program)` and `device.start()`, then publish values.

### Outputs: show a value

```python
from pihub import StringProperty

temperature = weather.output(id="temperature", type="Gauge",
                             properties=[StringProperty("title", default="Temperature"), StringProperty("units", default="°C")])
temperature.set(21.5)                                 # one value
conditions = weather.output(id="conditions", type="Chart")
conditions.set_values({"temperature": 21.5, "humidity": 60})   # several at once, for a Chart
```

While the connection is down `set()` does not raise: the latest value is sent when it comes back. Values are only sent to the platform
while someone is looking at the device (see Direct connections below).

### Commands: run your code from the dashboard

```python
lights = program.command(id="lights-on", type="Button", properties=[StringProperty("title", default="Lights on")])

@lights.handler
def pressed(command):
    switch_the_relay_on()
    program.info("Lights on")          # a message in the dashboard's notification history
```

### Settings: a value the dashboard changes and the device remembers

```python
program.setting("mode", "Select", default="Auto", options=["Auto", "Day", "Night"], title="Mode",
                apply=lambda value: print("mode is now", value))
program.setting("brightness", "Slider", default=50, minimum=0, maximum=100, step=5, units="%")
program.setting("armed", "Toggle", default=False)
```

The device checks a new value (one out of range is refused), saves it so it survives a restart, publishes it back and then calls `apply`.

### Notifications

`program.info(...)`, `program.warning(...)` and `program.error(...)` appear on the dashboard. A warning stays until someone acknowledges it.

### The Camera

```python
from pihub.video import best_camera_source

camera = program.output(id="camera", type="Camera")
camera.use(best_camera_source())     # the Pi's camera (its hardware encoder, or software encoding where there is none); anywhere else, a moving test pattern
camera.recorder_if_possible()        # adds a Record button when the camera can be recorded; recordings and snapshots are saved on the device
```

`best_camera_source()` uses the Pi's hardware H.264 encoder where there is one (the Pi Zero to the Pi 4: cheap, about 5% of a Pi 3A+). Where there is none (a Pi 5) it encodes the Pi camera on the CPU with
libx264 (`SoftwareEncodedCameraSource`: about 40% of one core of a Pi 3A+ at 640x480, and limited to 640x480 until a Pi 5 has been measured). Both hand on H.264 packets, so both stream, take snapshots
and record (`camera.recorder_if_possible()`, and the Motion Trap). If PyAV has no libx264 it falls back to raw frames that `aiortc` encodes: streaming and snapshots only, no recording. On a computer with no camera it is a moving test pattern;
on a Raspberry Pi that cannot use its camera it raises `CameraUnavailable` saying what to check, rather than showing a test pattern as if it were the camera. `source.description` says which one it chose.

The picture goes straight from the device to the browser and only while someone is watching, so an idle camera costs nothing.

## Examples

The source repository has small programs to copy, each a single file that connects to the real platform:

- `examples/widget_showcase.py` - every widget the dashboard can draw, in one program (the best place to start copying from)
- `examples/weather_station.py` - gauges and a chart
- `examples/greenhouse.py` - two gauges, a button that runs your code, and a warning
- `examples/birdbox.py` - a Pi camera on the dashboard

and, in `apps/`, complete programs to run as they are: a live bird-box camera, a GPIO panel (a button in, an LED out) and a motion-triggered camera trap.

## Command Line

```bash
pihub setup                      # link this machine with a code (headless onboarding)
pihub register TOKEN --device-id ID   # exchange a registration token you already have
pihub camera setup               # tell this machine what camera it has (a NoIR camera needs this)
pihub camera show
pihub install my_program.py      # link this machine if needed, then run the program at boot (one step)
```

`camera setup` looks at the camera's picture, asks whether it is a **NoIR** camera (no infrared filter) and saves the answer in
`camera.json` beside `config.json`. A NoIR camera is strongly pink until it is told, because libcamera picks its colour tuning by the
sensor's name and cannot tell the two apart. Every camera program then uses the answer by itself; `--noir` / `--normal` skip the
question, and `PiCameraSource(tuning=...)` / `HardwareCameraSource(tuning=...)` override it. Run it with the same `XDG_CONFIG_HOME`
as a service that uses its own. If nobody runs it, a program that calls `watch_camera_colour(source, device.information.warning)` (the sample app
does) raises one warning, from Device Information, the first time a real camera's picture is strongly pink; it points at the "Camera is NoIR" toggle there.

`setup` shows a link code, waits while the owner enters it in the web app,
asks for confirmation, then registers under a platform-issued `deviceId`.
Afterwards `Device()` (no arguments) loads that id from
`~/.config/thepihub/config.json`; `Device(id=...)` still works unchanged.
The server comes from `--server`, else `$PIHUB_SERVER`, else the default.

## Grouping components (a hint)

Any component can say it belongs with others: `output(..., group="Camera")`, `command(...)` and `setting(...)` take the same argument. It is sent as an
ordinary `group` property and it is only a **hint**: the dashboard shows a section's ungrouped components first, then each group under a small
heading, and is free to ignore it. Device Information puts the camera's sensor and its settings (Resolution, Frame rate, Bitrate, Camera is NoIR)
in the group `"Camera"`.

## Device Information (automatic)

A device that built its own connection (after `pihub setup`) also gets a **Device Information** program without any code of its own: it
is registered first, and shows the Pi model, hostname, OS, SDK version, uptime, CPU usage, temperature, memory, disk, power supply
(under-voltage or throttling), Wi-Fi signal, and the camera sensor if the device has a camera. Each field appears only if its source is
on that machine (a plain Linux server gets fewer). The live readings are taken only while someone is viewing the device. It is read-only
and does not include the serial number or machine id.

```python
Device()                                              # on (a real device)
Device(device_information=False)                      # off
Device(device_information=DeviceInformation(show_ip_address=True))   # on, plus the IP address (off by default)
```

A device given its own transport (a simulation or a test) does not get it unless you ask.

## Alerts from the machine's vitals

Device Information also **watches the vitals itself**, once a minute, whether or not anyone is viewing (about 0.01% of a Pi Zero W's one core), and
sends a notification from Device Information when something is wrong: a **warning** and an **error** level for CPU temperature (70 and 80 °C), CPU
usage (90 and 98%, over five checks in a row, so a brief spike is not an alert), memory (90 and 97%) and disk (85 and 95%). The dashboard asks the user
to acknowledge both.

It says so **once per episode**, not at every check: a reading that stays high says nothing more, and it must fall a margin below the level before it
counts as recovered (so a reading hovering around a level cannot flap); recovery is an information notice. The levels are provisional, a first guess.

```python
DeviceInformation(alerts=False)                       # no alerts
DeviceInformation(alerts=[AlertRule(...)])            # your own levels (pihub.device_alerts)
DeviceInformation(alert_seconds=300)                  # check every 5 minutes instead
```

Any section can run its own periodic work the same way: `section.every(seconds, func)` (before `start()`; first run one interval after start).

## Direct connections (WebRTC)

A browser can connect straight to the device (the WebRTC library is part of the install; see Install). Then every runtime value the device publishes also goes to that browser over
its DataChannel (a viewer that has just connected is first sent the latest value of everything), and the browser may send commands the same way. A command from a
viewer reaches your handlers exactly like one from the platform (`@command.handler`), on its own thread, so a slow handler cannot stall the connection. The device only
accepts one while that viewer's session is alive, and limits it: a burst of 50, then 30 a second per viewer; a viewer is told at once whether it was handed over (202) or
refused, but not whether it was done.

**The platform only gets a value while it is needed.** With nobody watching, or every viewer connected directly, runtime values are not sent to the platform at all (notifications, registration and
signalling still are), which is what keeps a device's cost down. A viewer that is still connecting, or has failed, is served through the platform, and the moment that starts the device sends it a
snapshot of the latest of everything. So the platform's stored copy of a value is only as fresh as the last time it was needed: `GET /devices/{id}/runtime` says "the last value the platform received".
A device without the extra sends everything to the platform, as it always did. The device and the page each say what they support when the channel opens, and each relies on the direct path only once
the other has said it can, so an older page (still open in a tab) or an older device is left on the platform's path.

## Staying Connected

A device that built its own connection (the normal case, after `pihub setup`)
reconnects by itself when the connection drops - a server redeploy, a wifi
blip, a pulled cable. It retries forever with a growing delay (1s to 60s,
jittered), and on reconnect re-announces itself and re-sends the latest value
of each output, so the dashboard is never left empty.

```python
Device()                      # reconnects (its own connection)
Device(id="x", transport=t)   # does not (you supplied the transport)
Device(reconnect=False)       # switched off explicitly
```

While reconnecting, `Output.set()` does not raise; the latest value is sent
once the connection is back. A refused login (HTTP 403) is retried with a
clear error in the log and never given up on - the platform gives the same
answer for a bad secret and for a rate-limited device.

## GPIO pins (sensors, LEDs, buttons)

Apps drive pins with gpiozero (`pip install "thepihub[gpio]"`). Call `pihub.gpio.prepare_pins()` before making the first gpiozero
device: gpiozero's driver on current Raspberry Pi OS, `lgpio`, comes from apt (`python3-lgpio`) and does not build under pip, so a
venv without system packages would otherwise fall back to gpiozero's own driver, which cannot watch a pin for changes on current
kernels (a `Button` or `DigitalInputDevice` fails with `OSError: [Errno 22]`). `prepare_pins()` lets the venv see the system's
`lgpio`, as the camera code already does for picamera2.

## Running At Boot (opt-in)

Nothing is installed unless you ask:

```bash
pihub service install apps/birdbox_live.py [--name N] [--config-home DIR]
pihub service status [N]
pihub service uninstall N
```

It creates a systemd *user* service that restarts the program if it dies.
To start at boot with nobody logged in, run once:
`sudo loginctl enable-linger <user>` (the command prints this when needed).

## Licence

Apache License 2.0; see `LICENSE` and `NOTICE`.
