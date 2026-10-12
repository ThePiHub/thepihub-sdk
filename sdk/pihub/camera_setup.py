"""`pihub camera setup` and `pihub camera show`: tell this machine what kind of camera it has.

    pihub camera setup            find the camera, look at its picture, ask whether it is a NoIR camera, save the answer
    pihub camera setup --noir     say so, without being asked (no question)
    pihub camera setup --normal   say it is a normal camera (no question)
    pihub camera show             what is saved

Why: see `pihub.camera_config`. A NoIR camera is pink until it is given its own tuning, the camera cannot say which kind it is, and
this is the one place it is asked. The answer is saved, and every camera program (`PiCameraSource`, `HardwareCameraSource`, so
`apps/birdbox_live.py` and a service) uses it without being told. Needs the camera free: stop a program that is using it first.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from pihub.camera_config import default_camera_config_path, load_camera_state, noir_tuning_for, save_camera_tuning
from pihub.video.colour import colour_cast, describe_cast, looks_pink  # noqa: F401  (shared with the first-start warning)
from pihub.video.sources import CameraUnavailable, import_picamera2, new_camera

SETTLE_SECONDS = 2.0  # the camera's automatic white balance needs a moment after it starts
SIZE = (640, 480)


def _detect_sensor_here() -> Optional[str]:
    cameras = import_picamera2().Picamera2.global_camera_info()
    if not cameras:
        return None
    return str(cameras[0].get("Model", "")).lower() or None


def detect_sensor() -> Optional[str]:
    """The first camera's sensor name as libcamera reports it (`imx219`), or `None` if no camera is found (or it cannot be asked).

    Asked in a SEPARATE process, on purpose. Asking libcamera starts its camera manager, which loads the camera's colour tuning at that moment
    (the default one) and keeps it for the life of the process: a camera opened later with the saved NoIR tuning would silently get the default,
    and be pink. (Found on a Zero W: the first Start after the program launched was pink although NoIR was saved.) A child process asks and exits.
    """
    code = "from pihub.camera_setup import _detect_sensor_here as ask\nprint(ask() or '')"
    try:
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]  # libcamera also logs; the answer is the last line
    return lines[-1].strip().lower() if lines else None


def measure_cast(tuning: str) -> Tuple[float, float]:
    """Take one picture with this tuning (`""` = the camera's own default, ignoring anything saved) and return its colour cast."""
    Picamera2 = import_picamera2().Picamera2
    camera = None
    try:
        camera = new_camera(Picamera2, tuning)
        camera.configure(camera.create_video_configuration(main={"size": SIZE, "format": "RGB888"}))
        camera.start()
        time.sleep(SETTLE_SECONDS)
        return colour_cast(camera.capture_array("main"))
    except Exception as error:  # no camera, or another program holds it
        raise CameraUnavailable(f"could not take a picture: {error}") from error
    finally:
        if camera is not None:
            try:
                camera.stop()
                camera.close()
            except Exception:
                pass


def run_camera_setup(
    noir: Optional[bool] = None,
    *,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
    err: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    config_path: Optional[Path] = None,
    detect: Callable[[], Optional[str]] = detect_sensor,
    measure: Callable[[str], Tuple[float, float]] = measure_cast,
    tuning_for: Callable[[str], Optional[str]] = noir_tuning_for,
) -> int:
    """Ask (or be told) whether the camera is a NoIR one, and save the answer. Returns a process exit code.

    `noir=True` / `False` is the answer given up front (`--noir` / `--normal`); `None` asks.
    """
    try:
        sensor = detect()
    except Exception as error:
        err(f"error: could not look for a camera: {error}")
        return 1
    if sensor is None:
        err("error: no camera found. Check the ribbon cable, then `rpicam-hello --list-cameras`.")
        return 1
    out(f"Camera: {sensor}")

    if sensor.endswith("_noir"):
        out("libcamera already recognises this as a NoIR camera, so there is nothing to set.")
        save_camera_tuning(None, config_path)
        return 0
    tuning = tuning_for(sensor)
    if tuning is None:
        out(f"libcamera has no NoIR tuning for the {sensor} sensor, so there is nothing to choose. Saved: a normal camera.")
        save_camera_tuning(None, config_path)
        return 0

    before: Optional[Tuple[float, float]] = None
    if noir is None:
        out("Looking at the camera's picture...")
        try:
            before = measure("")  # "": the camera's own default, whatever is already saved
        except CameraUnavailable as error:
            err(f"error: {error}\nIf another program is using the camera (a running birdbox_live, or a service), stop it and try again.")
            return 1
        pink = looks_pink(*before)
        out(f"With the normal colour setting the picture is {describe_cast(*before)} (red/green {before[0]:.2f}, blue/green {before[1]:.2f}).")
        if pink:
            out("That is what a NoIR camera (one with no infrared filter) looks like under the normal setting.")
        question = "Is this a NoIR camera? [Y/n] " if pink else "Is this a NoIR camera (no infrared filter, 'NoIR' on the box)? [y/N] "
        try:
            answer = input_fn(question).strip().lower()
        except EOFError:
            answer = None
        if answer is None:
            out("Cancelled. Nothing was saved.")
            return 1
        noir = answer in {"y", "yes"} if answer else pink

    if not noir:
        path = save_camera_tuning(None, config_path)
        out(f"Saved: a normal camera ({path}).")
        return 0

    try:
        after = measure(tuning)
    except CameraUnavailable as error:
        out(f"Could not check the result ({error}); saving it anyway.")
    else:
        was = f"was {describe_cast(*before)}, " if before is not None else ""
        out(f"With the NoIR tuning ({tuning}) the picture {was}is now {describe_cast(*after)} (red/green {after[0]:.2f}, blue/green {after[1]:.2f}).")
        if looks_pink(*after):
            out("It is still strongly pink: this may not be a NoIR camera, or it needs a different tuning. Saved anyway; "
                "`pihub camera setup --normal` undoes it.")
    path = save_camera_tuning(tuning, config_path)
    out(f"Saved: {tuning} ({path}).")
    out("Every camera program on this machine now uses it. A service that runs with its own XDG_CONFIG_HOME reads its own copy of "
        "this file, so run this command with the same XDG_CONFIG_HOME.")
    return 0


def run_camera_show(*, out: Callable[[str], None] = print, config_path: Optional[Path] = None) -> int:
    path = config_path if config_path is not None else default_camera_config_path()
    state = load_camera_state(config_path)
    if state.tuning:
        out(f"Camera tuning: {state.tuning}  ({path})")
    elif state.asked:
        out(f"Camera tuning: none, a normal camera  ({path})")
    else:
        out(f"Camera tuning: not set, so the camera's own default  (`pihub camera setup` asks; would be {path})")
    return 0
