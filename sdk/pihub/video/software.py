"""A Pi camera encoded to H.264 by the CPU (libx264), and handed on as packets like the hardware source: so it can be RECORDED as well as streamed.

The Pi 5 has no hardware H.264 encoder. `PiCameraSource` gives it live video and snapshots, but its frames are raw (`aiortc` encodes them), so there is no Record
button and the Motion Trap cannot run. This source is `HardwareCameraSource` with one thing swapped: picamera2's `LibavH264Encoder` (libav's libx264, in a thread)
instead of the V4L2 hardware `H264Encoder`. The packet buffer, the packet listeners a recorder uses, snapshots and the settings are all the same, because none of them
depend on which encoder made the packets.

It costs what software encoding costs (about 57% of a Pi 3A+ at 640x480, so `max_pixels` stays there until a Pi 5 has been measured), and unlike the hardware encoder it can
run on any Pi, which is how it was proven on a Pi 3A+ before a Pi 5 was to hand.
"""

from __future__ import annotations

from typing import Any

from pihub.video.hardware import HardwareCameraSource
from pihub.video.sources import import_picamera2


def libx264_available() -> bool:
    """Whether PyAV can encode H.264 in software (libx264). On Raspberry Pi OS: `sudo apt install python3-av`."""
    try:
        import av

        av.codec.Codec("libx264", "w")
        return True
    except Exception:  # PyAV missing, or built without libx264
        return False


class SoftwareEncodedCameraSource(HardwareCameraSource):
    """A Pi camera, encoded to H.264 on the CPU, as packets: streams, takes snapshots AND records."""

    description = "software H.264, recordable: the Pi's camera encoded on the CPU"
    encoder_name = "software (libx264) encoder"
    max_pixels = 640 * 480  # software H.264 on a Pi: about 57% of a Pi 3A+ at 640x480. Raise it once a Pi 5 has been measured.

    @staticmethod
    def available() -> bool:
        """Whether this machine can encode a Pi camera in software: picamera2 with `LibavH264Encoder`, at least one camera listed, and libx264 in PyAV."""
        try:
            picamera2 = import_picamera2()
            if getattr(picamera2, "LibavH264Encoder", None) is None:
                return False
            if len(picamera2.Picamera2.global_camera_info()) == 0:
                return False
        except Exception:  # no library, no libcamera, or nothing attached
            return False
        return libx264_available()

    def _make_encoder(self, picamera2: Any) -> Any:
        return picamera2.LibavH264Encoder(
            bitrate=self.bitrate,
            repeat=True,  # sequence headers with every keyframe: a viewer can join at any of them
            iperiod=max(1, int(round(self.fps * self.keyframe_seconds))),
            framerate=self.fps,
            profile=self.profile,  # libx264 matches "constrained baseline" by name; "zerolatency" (picamera2's own setting) means no B-frames
        )

    def request_keyframe(self) -> None:
        """Deliberately does nothing: the regular keyframes (a second apart) are all a viewer needs, so it waits at most a second to join.

        picamera2's `LibavH264Encoder.force_key_frame()` exists, but on a real Pi 3A+ (picamera2 with PyAV 14.2.0, 2026-09-28) it BREAKS THE STREAM: it sets
        `frame.pict_type = "I"`, which PyAV rejects (`TypeError: an integer is required`) inside picamera2's own request thread, which then stops encoding: the live picture
        freezes and a recording gets no video. Found only by running on the real camera; the tests keep it from being "fixed" back.
        """
