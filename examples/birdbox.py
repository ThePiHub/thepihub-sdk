"""Bird Box - the Camera component: live video with buttons to record and take a snapshot.

Copy this file anywhere and run it:

    python birdbox.py

If this is the first time you have connected from this device, it prints a link and a code: open the link
while logged in to ThePiHub and approve it, then answer y when the terminal asks you to confirm the account.
The device is yours and gets its id. Every run after that connects straight away. Ctrl+C stops it.

On a Raspberry Pi with a camera this shows the Pi's camera; anywhere else, a moving test pattern. Where the Pi has a hardware video encoder it does the encoding; where it has none (a Pi 5), the CPU does, at the CPU's cost. Either way the picture streams, you can take snapshots and
you can record.
It needs the video pieces (the plain `pip install thepihub` brings PyAV; on a 32-bit Pi there is no pip build of PyAV, so install it with apt (`sudo apt install python3-picamera2 python3-av python3-aiortc`) and create the virtualenv with `--system-site-packages`).
The camera only runs while someone is watching or recording. Pictures go to ~/thepihub-snapshots
and recordings to ~/thepihub-recordings, on this device.
"""

from __future__ import annotations

import time
from typing import Optional

from pihub import Device, Program
from pihub.device import Transport
from pihub.video import best_camera_source


def main(transport: Optional[Transport] = None) -> Device:
    # `transport` is for the tests. Left out, the device connects to the real server.
    device = Device(transport=transport)

    birdbox = Program(id="birdbox", name="Bird Box")

    # The Camera component draws the video with its own buttons: stream, snapshot, record, full screen.
    camera = birdbox.output(id="camera", type="Camera")
    camera.use(best_camera_source())  # the Pi's camera (hardware or software encoding); anywhere else, a moving test pattern
    camera.recorder_if_possible()  # adds the Record button when the camera can be recorded

    temperature = birdbox.output(id="temperature", type="Gauge")

    # The camera has nothing to do while nobody is watching: these say when someone starts or stops.
    @birdbox.on_first_user_connected
    def viewer_arrived(payload: dict) -> None:
        birdbox.info("Viewer connected")

    @birdbox.on_last_user_disconnected
    def viewers_left(payload: dict) -> None:
        birdbox.info("Last viewer disconnected")

    device.add_program(birdbox)
    device.start()

    temperature.set(22.5)  # replace with a real sensor reading

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
