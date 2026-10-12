"""Greenhouse - two Gauges, a Button that runs your code, and notifications.

Copy this file anywhere and run it:

    python greenhouse.py

If this is the first time you have connected from this device, it prints a link and a code: open the link
while logged in to ThePiHub and approve it, then answer y when the terminal asks you to confirm the account.
The device is yours and gets its id. Every run after that connects straight away. Ctrl+C stops it.
"""

from __future__ import annotations

import time
from typing import Optional

from pihub import Device, Program
from pihub.device import Transport
from pihub.protocol.messages import CommandMessage


def main(transport: Optional[Transport] = None) -> Device:
    # `transport` is for the tests. Left out, the device connects to the real server.
    device = Device(transport=transport)

    greenhouse = Program(id="greenhouse", name="Greenhouse")

    temperature = greenhouse.output(id="temperature", type="Gauge")
    humidity = greenhouse.output(id="humidity", type="Gauge")
    water_plants = greenhouse.command(id="waterPlants", type="Button")

    @water_plants.handler
    def water(command: CommandMessage) -> None:
        # Your code goes here (open a valve, switch a pump). Then tell the dashboard.
        print("Water plants pressed")  # shows here, in the terminal
        greenhouse.info("Plants watered")  # and in the dashboard's Notification history

    device.add_program(greenhouse)
    device.start()

    # Replace these with real sensor readings.
    temperature.set(24.0)
    humidity.set(38.0)

    # A warning shows on the dashboard until someone acknowledges it.
    greenhouse.warning("Soil moisture low")

    return device


if __name__ == "__main__":
    device = main()  # pragma: no cover
    print(f"{device.id} is online. Ctrl+C to stop.")  # pragma: no cover
    try:  # pragma: no cover
        while True:
            time.sleep(1)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:  # pragma: no cover
        device.stop()
