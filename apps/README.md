# Apps: projects you can run as they are

Each file here is a whole program for a Raspberry Pi: copy nothing, change nothing, run it. The docstring at the top of each one says
what it does, what hardware it needs, how to wire it and how to run it (`--help` prints it too).

| Project | What it does | Hardware | Status |
|---|---|---|---|
| [`birdbox_live.py`](birdbox_live.py) | The Pi's camera on the dashboard: live video, Record (to the Pi's own storage) and Snapshot, from one Camera component | A Pi and a camera | In use (on a Pi Zero W) |
| [`motion_trap.py`](motion_trap.py) | A camera trap: records video when a motion sensor sees movement, tells the dashboard, and stops once things have been still for a while. An infrared light is on only while the camera is, and the Pi's own lights are off, so nothing glows at the wildlife. Live video too | A Pi with a camera (hardware H.264: not a Pi 5; NoIR for night), a PIR motion sensor; optionally an infrared illuminator and a transistor | Built and tested in `--simulate` and with a real PIR sensor on a Pi 3A+ (24 recordings); the IR light is not yet wired |
| [`gpio_panel.py`](gpio_panel.py) | A push button and an LED on the dashboard: the button shows Pressed or Released live, a Toggle switches the LED. The smallest complete example of digital inputs and outputs, plain gpiozero, to copy for your own (a door switch, a relay, a buzzer) | Any Pi with the 40-pin header, a push button, an LED and a 330 ohm resistor | Built, tested in `--simulate`, and its pins opened and driven on the real Pi 3A+; a real button press not yet seen |

## Conventions

- One file each, with the docstring header described above.
- The same shape throughout: the first run, at a terminal, prints a link and a code to approve and the platform assigns
  the device's id (nothing to write: `Device()` does it), with `--server` and `--simulate` wherever there is hardware, so the app runs on any computer (gpiozero's pretend pins, a test-pattern camera).
- A test each, in simulate mode, in `tests/apps/`, so they keep working.
- `examples/` is different: small programs for people writing their own to copy and run against the real server (the
  first run registers by link code, and the platform assigns the device id), tested as documentation. A project someone would run as it is belongs here.
