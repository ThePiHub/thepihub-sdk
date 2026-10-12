"""Colour of a camera's picture, and the one-time warning for a NoIR camera that was never set up.

A NoIR camera (no infrared filter) is strongly pink under libcamera's default tuning until `pihub camera setup` tells this machine
what it has (see `pihub.camera_config`). A user who never runs that sees a hot-pink picture and nothing to explain it. So the first
time the camera runs, a program can look at one picture and, if it is strongly pink and the camera was never set up, raise a single
notification that says what to do: `watch_camera_colour(source, device.information.warning)`.

It never decides anything: it only tells the user, once. The thresholds are provisional (two pink measurements from one Zero W,
5.59 and 2.96 in different scenes; neutral is 1.00), and a red room or warm light tints a normal camera too, which is why it is
worded "if this is a NoIR camera".

Nothing here imports `numpy`, `av` or `picamera2` until a picture is actually looked at.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable, Optional, Tuple

from pihub.camera_config import default_camera_config_path, load_camera_state, mark_camera_warned

logger = logging.getLogger("pihub.video.colour")

# Provisional: see the module docstring. They choose the default answer to `pihub camera setup`'s question and whether to warn.
PINK_RED_OVER_GREEN = 1.8
PINK_BLUE_OVER_GREEN = 1.3
# A picture whose BRIGHTEST channel averages below this (of 255) is too dark for its colour to mean anything: a covered lens is not "pink".
# (Not green alone: a strongly pink picture has almost no green and is far from dark. The Zero W's NoIR camera gave red 232, green 13, blue 142.)
MIN_BRIGHTNESS = 25.0
# The camera's automatic white balance needs a moment after it starts before its picture can be judged.
SETTLE_SECONDS = 3.0


def colour_cast(pixels: Any, bgr: bool = True) -> Tuple[float, float]:
    """(red/green, blue/green) of a picture, from each channel's average. 1.0 and 1.0 is neutral.

    `pixels` is an array of shape (..., 3). By default its channels are in `picamera2`'s "RGB888" byte order (blue, green, red);
    pass `bgr=False` for ordinary red, green, blue.
    """
    means = pixels.reshape(-1, 3).mean(axis=0)
    first, green, last = (float(value) for value in means)
    blue, red = (first, last) if bgr else (last, first)
    green = max(green, 1e-6)
    return red / green, blue / green


def looks_pink(red_over_green: float, blue_over_green: float) -> bool:
    return red_over_green >= PINK_RED_OVER_GREEN and blue_over_green >= PINK_BLUE_OVER_GREEN


def describe_cast(red_over_green: float, blue_over_green: float) -> str:
    if looks_pink(red_over_green, blue_over_green):
        return "strongly pink"
    if 0.8 <= red_over_green <= 1.25 and 0.8 <= blue_over_green <= 1.25:
        return "close to neutral"
    return "tinted"


def frame_cast(frame: Any) -> Optional[Tuple[float, float]]:
    """The colour cast of an `av.VideoFrame`, or `None` if it is too dark to judge."""
    pixels = frame.to_ndarray(format="rgb24")
    if float(pixels.reshape(-1, 3).mean(axis=0).max()) < MIN_BRIGHTNESS:
        return None
    return colour_cast(pixels, bgr=False)


def pink_warning_message(toggle: bool = False) -> str:
    """What to tell the user. `toggle` is True when the dashboard offers "Camera is NoIR" (Device Information > Camera): that is the easy way,
    so the message points there. Otherwise, `pihub camera setup` on the Pi is the way."""
    if toggle:
        return (
            "The camera's picture is strongly pink. If this is a NoIR camera (no infrared filter), turn on \"Camera is NoIR\" "
            "under Camera in Device Information, then stop and start the camera."
        )
    message = "The camera's picture is strongly pink. If this is a NoIR camera (no infrared filter), run `pihub camera setup` on the Pi to fix the colour."
    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        message += f" (Run it with XDG_CONFIG_HOME={config_home}.)"
    return message


def watch_camera_colour(
    source: Any,
    notify: Callable[[str], None],
    *,
    config_path: Optional[Any] = None,
    settle_seconds: float = SETTLE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Any], None]:
    """Warn ONCE, through `notify`, if this camera's picture is strongly pink and the camera was never set up.

    Attach it to a program's video source, e.g. `watch_camera_colour(camera_source, device.information.warning)`, so the warning comes from Device Information, next to the camera's settings. The check runs on a background
    thread each time the camera starts, until the user has answered `pihub camera setup` or has been warned once (both are saved in
    `camera.json`, so a restart does not repeat it). It does nothing for a source that is not a real camera (a test pattern's colours
    mean nothing), and nothing if a tuning was given explicitly (the user has already chosen). A picture that is not pink, or too dark
    to judge, or a camera that stops before it settles, only means "not yet": it looks again on the next start.

    Returns the check itself (called with the source), mainly so tests can run it without a thread.
    """

    def check(source_: Any) -> None:
        if not getattr(source_, "real_camera", False) or getattr(source_, "tuning", None) is not None:
            return
        state = load_camera_state(config_path)
        if state.asked or state.warned:
            return
        sleep(settle_seconds)
        try:
            cast = frame_cast(source_.grab_still())
        except Exception:  # the camera stopped before it settled, or a frame could not be read: try again next start
            logger.debug("could not look at the camera's picture", exc_info=True)
            return
        if cast is None or not looks_pink(*cast):
            return
        try:
            notify(pink_warning_message(toggle=getattr(source_, "noir_setting", False) is True))
        except Exception:  # not connected right now: try again next start
            logger.debug("could not send the pink-picture notification", exc_info=True)
            return
        if not mark_camera_warned(config_path):
            logger.debug("could not save that the warning was sent to %s", config_path or default_camera_config_path())

    def on_start(source_: Any) -> None:
        threading.Thread(target=check, args=(source_,), daemon=True, name="camera-colour-check").start()

    source.add_start_listener(on_start)
    return check
