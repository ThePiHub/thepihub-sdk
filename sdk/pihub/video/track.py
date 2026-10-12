"""The bridge between a `VideoSource` and `aiortc`: a `MediaStreamTrack` whose frames come from the source.

Imported lazily (only when a peer connection actually needs a track), so `import pihub.video` never
imports `aiortc`.
"""

from __future__ import annotations

import asyncio
import time
from fractions import Fraction
from typing import Any

from aiortc.mediastreams import MediaStreamError, VideoStreamTrack

VIDEO_CLOCK_RATE = 90000


class SourceTrack(VideoStreamTrack):
    """Pulls frames from `source.read()` on a worker thread and stamps them for RTP.

    `aiortc` calls `recv()` when it wants the next frame to encode. Two things about its sender shape this:

    * **A track that raises `MediaStreamError` ends the sender for good** (it cannot be restarted on that
      connection). So a release does not stop the track under the sender: it swaps the track out
      (`replaceTrack(None)`, which the sender idles through), waits for any frame still being read
      (`wait_idle`), and only then stops the track and the camera.
    * **RTP timestamps must keep increasing across a restart** or the browser drops the frames. `epoch` is
      one monotonic time shared by every track on a peer connection, so a second start continues the clock.
    """

    def __init__(self, source: Any, epoch: float) -> None:
        super().__init__()
        self._source = source
        self._epoch = epoch
        self._idle = asyncio.Event()
        self._idle.set()
        self.frames = 0

    async def wait_idle(self, timeout: float = 3.0) -> None:
        """Wait for a frame that is being read right now (a camera read takes up to one frame time)."""
        try:
            await asyncio.wait_for(self._idle.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def recv(self) -> Any:
        if self.readyState != "live":
            raise MediaStreamError
        loop = asyncio.get_running_loop()
        self._idle.clear()
        try:
            frame = await loop.run_in_executor(None, self._source.read)
        except Exception as error:
            self.stop()
            raise MediaStreamError from error
        finally:
            self._idle.set()
        if self.readyState != "live":
            raise MediaStreamError
        # A raw frame or an already-encoded packet (`source.encoded`): either way it is stamped with time since the
        # shared epoch. aiortc encodes a frame, and just packetises a packet; it needs the same `pts` for both.
        frame.pts = int((time.monotonic() - self._epoch) * VIDEO_CLOCK_RATE)
        frame.time_base = Fraction(1, VIDEO_CLOCK_RATE)
        self.frames += 1
        return frame
