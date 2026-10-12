"""GPIO Panel - a button in and an LED out, on the dashboard: the smallest complete example of digital inputs and outputs.

**What it does.** A push button on one pin shows on the dashboard as "Pressed" or "Released", live. A Toggle on the
dashboard switches an LED on another pin. That is all of digital I/O: read a pin, drive a pin. To make your own,
copy this file and change what the two pins mean (a door switch, a relay, a buzzer, a float switch).

**Nothing pihub-specific touches the pins.** They are plain gpiozero (`Button`, `LED`): the app only connects a
gpiozero event to a dashboard component and a dashboard component to a gpiozero call. gpiozero is the whole GPIO
story; pihub adds nothing to it except `prepare_pins()`, which lets a virtual environment find Raspberry Pi OS's pin
driver.

**Hardware.** A Raspberry Pi (any model with the 40-pin header), a push button and an LED:

    Button  one leg -> GPIO 17 (pin 11)     the other leg -> GND (pin 9)        (no resistor: the Pi's own pull-up is on)
    LED     GPIO 27 (pin 13) -> 330 ohm -> LED anode (long leg); LED cathode (short leg) -> GND (pin 14)

An LED needs its resistor: a pin gives about 3.3 V and cannot limit the current itself. For something that draws more
than a few mA (a relay board, a buzzer, a motor), a pin cannot drive it directly: use a transistor or a relay board
made for 3.3 V logic, as the motion trap does for its infrared light. Never connect a pin to more than 3.3 V.

**The LED starts off** every time the app starts, and goes off again when it stops, whatever the Toggle was left on:
an output that quietly comes back on after a power cut is not what most things you would switch want. (The Toggle is
not saved, for the same reason.)

**Run it.** Just run it:

    .venv/bin/python apps/gpio_panel.py

The first time, it prints a link and a short code: open the link while you are logged in, approve it, and answer `y`
when the terminal asks you to confirm the account. That makes the Pi yours, and the platform gives it its id. Every run
after that connects straight away. (`pihub setup` does the same linking on its own, if you would rather do that first.)

**No hardware?** `--simulate` uses gpiozero's pretend pins, so it runs on any computer; press "Test press" on the
dashboard to press the pretend button; the Toggle drives a pretend LED. `pip install "thepihub[gpio]"`
if gpiozero is missing (Raspberry Pi OS already has it; a venv made without `--system-site-packages` does not). The pin
driver, `lgpio`, comes from Raspberry Pi OS (`python3-lgpio`) and is found from a venv as well.
"""

from __future__ import annotations

import argparse
import signal
import time
from typing import Any, List, Optional

from pihub import Device, Program


PROGRAM_ID = "gpio-panel"
PRESSED, RELEASED = "Pressed", "Released"
TEST_PRESS_SECONDS = 0.3


class GpioPanel:
    """The dashboard section: a Status for the button, a Toggle for the LED. `button` is a gpiozero `Button`, `led` a gpiozero `LED`
    (or anything with `when_pressed`, `when_released`, `is_pressed` and `on()`, `off()`). Build it before `device.start()`, then call `start()`."""

    def __init__(self, device: Device, button: Any, led: Any, *, simulated_press: Optional[Any] = None) -> None:
        self.device = device
        self.button = button
        self.led = led
        self.program = Program(id=PROGRAM_ID, name="GPIO Panel")

        self.state = self.program.output(id="button", type="Status")
        button.when_pressed = lambda: self.state.set(PRESSED)
        button.when_released = lambda: self.state.set(RELEASED)

        # persist=False: the LED is off at every start, so the Toggle must not remember "on" (see the docstring at the top).
        self.light = self.program.setting("led", "Toggle", default=False, title="LED", persist=False, apply=self._light_changed)

        if simulated_press is not None:
            press = self.program.command(id="test-press", type="Button")
            press.handler(lambda command: self.test_press(simulated_press))
        device.add_program(self.program)

    def start(self) -> None:
        """After `device.start()`: show where the button is now, and make sure the LED is off."""
        self.led.off()
        self.state.set(PRESSED if self.button.is_pressed else RELEASED)

    def close(self) -> None:
        self.led.off()

    def _light_changed(self, value: Any) -> None:
        self.led.on() if value else self.led.off()

    @staticmethod
    def test_press(pin: Any) -> None:
        """Press and release the pretend button, by driving its mock pin the way a real one would go: low while pressed (pull-up wiring)."""
        pin.drive_low()
        time.sleep(TEST_PRESS_SECONDS)
        pin.drive_high()


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", help="the platform to register with the first time (default: $PIHUB_SERVER, else https://thepihub.com); ignored once registered")
    parser.add_argument("--simulate", action="store_true", help="no hardware: pretend GPIO pins, and a Test press button on the dashboard")
    parser.add_argument("--button-pin", type=int, default=17, help="the GPIO the button is wired to, the other leg to GND (default: 17)")
    parser.add_argument("--led-pin", type=int, default=27, help="the GPIO the LED (through its resistor) is wired to (default: 27)")
    return parser.parse_args(argv)


def build(args: argparse.Namespace, transport: Any = None) -> GpioPanel:
    """The device, its button and LED, and the panel, NOT yet started. `transport` is for tests."""
    try:
        from gpiozero import LED, Button, GPIOZeroError
        from gpiozero import Device as Pins
    except ImportError:
        raise SystemExit('error: this app needs gpiozero: pip install "thepihub[gpio]" (Raspberry Pi OS has it for its own Python)')
    from pihub.gpio import PIN_DRIVER_HELP, prepare_pins

    if args.button_pin == args.led_pin:
        raise SystemExit("error: the button and the LED cannot use the same pin")
    if args.simulate:
        from gpiozero.pins.mock import MockFactory

        Pins.pin_factory = MockFactory()
    else:
        prepare_pins()  # a venv without system packages: the pin driver is Raspberry Pi OS's own

    try:
        # At a terminal, the first run links this machine by code and the platform assigns its id; every run after connects at once.
        device = Device(transport=transport, server=args.server)
    except ValueError as error:
        raise SystemExit(f"error: {error}")

    try:
        button = Button(args.button_pin, bounce_time=0.05)  # pulled up by the Pi; pressed = connected to GND; bounce_time ignores contact chatter
        led = LED(args.led_pin)
    except (GPIOZeroError, OSError) as error:
        raise SystemExit(f"error: {PIN_DRIVER_HELP} ({error})")
    return GpioPanel(device, button, led, simulated_press=button.pin if args.simulate else None)


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    panel = build(args)
    panel.device.start()
    panel.start()
    print(f"{panel.device.id} is online{' (simulated: press Test press on the dashboard)' if args.simulate else ''}. Ctrl+C to stop.")
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a service stop (systemd) ends the loop like Ctrl+C, so the LED is switched off
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping...")
        panel.close()
        panel.device.stop()
        panel.button.close()
        panel.led.close()


if __name__ == "__main__":
    main()  # pragma: no cover
