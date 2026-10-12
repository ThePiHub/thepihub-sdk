"""Weather Station - Gauges and a Chart, updated every few seconds.

Copy this file anywhere and run it:

    python weather_station.py

If this is the first time you have connected from this device, it prints a link and a code: open the link
while logged in to ThePiHub and approve it, then answer y when the terminal asks you to confirm the account.
The device is yours and gets its id. Every run after that connects straight away. Ctrl+C stops it.

The readings are random numbers standing in for a sensor: replace `_read_conditions()`.
"""

from __future__ import annotations

import random
import time
from typing import Dict, Optional

from pihub import Device, Program
from pihub.device import Transport


def _read_conditions() -> Dict[str, float]:
    """Random readings, standing in for real sensor hardware."""
    return {
        "temperature": round(random.uniform(15.0, 30.0), 1),
        "humidity": round(random.uniform(30.0, 90.0), 1),
        "pressure": round(random.uniform(980.0, 1050.0), 1),
    }


def main(transport: Optional[Transport] = None, cycles: Optional[int] = None, interval: float = 2.0) -> Device:
    """`transport` and `cycles` are for the tests: left out, it connects to the real server and runs until Ctrl+C."""
    device = Device(transport=transport)

    weather = Program(id="weather", name="Weather Station")

    temperature = weather.output(id="temperature", type="Gauge")
    humidity = weather.output(id="humidity", type="Gauge")
    pressure = weather.output(id="pressure", type="Gauge")
    conditions = weather.output(id="conditions", type="Chart")

    device.add_program(weather)
    device.start()

    done = 0
    try:
        while cycles is None or done < cycles:
            readings = _read_conditions()

            # One value per Gauge.
            temperature.set(readings["temperature"])
            humidity.set(readings["humidity"])
            pressure.set(readings["pressure"])

            # Several values at once, for the Chart.
            conditions.set_values(readings)

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
