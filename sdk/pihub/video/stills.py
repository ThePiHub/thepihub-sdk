"""Still pictures from a video source: what a "Snapshot" button should really do.

`capture_still()` saves one JPEG and returns its path, or raises `CameraUnavailable` and leaves no file. It is
honest by construction: the path it returns is a file that exists and is not empty, so a program can say "saved"
only when it was.

It shares the camera properly. If live video is streaming, the picture is taken from the running camera (a recent
frame of the stream, or a capture from the same camera for a hardware-encoded stream) and the camera is left running
for the viewer. If the camera is idle it is opened just long enough to take the picture
(a few frames are read first, so auto-exposure has settled) and then closed again. It never stops a camera
that someone else is using.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional, Union

from pihub.video.sources import SETTLE_FRAMES, CameraUnavailable, VideoSource  # noqa: F401 (re-exported)


def save_jpeg(frame: Any, path: Path, quality_scale: Optional[int] = None) -> None:
    """Write `frame` (an `av.VideoFrame`) to `path` as a JPEG, using the encoder PyAV already ships."""
    import av

    with av.open(str(path), "w") as output:
        stream = output.add_stream("mjpeg")
        stream.width, stream.height = frame.width, frame.height
        stream.pix_fmt = "yuvj420p"
        if quality_scale is not None:
            stream.codec_context.options = {"qscale": str(quality_scale)}
        for packet in stream.encode(frame.reformat(format="yuvj420p")):
            output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)


def _unused_name(directory: Path, prefix: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = directory / f"{prefix}-{stamp}.jpg"
    n = 1
    while path.exists():  # two pictures in the same second
        n += 1
        path = directory / f"{prefix}-{stamp}-{n}.jpg"
    return path


def capture_still(source: VideoSource, directory: Union[str, Path], prefix: str = "snapshot") -> Path:
    """Take one picture from `source` and save it in `directory`. Returns the file's path."""
    folder = Path(directory).expanduser()
    folder.mkdir(parents=True, exist_ok=True)

    source.start()  # counted: shares a running camera (a live stream), or opens an idle one for just this
    try:
        frame = source.grab_still()  # a live stream's own frame if there is one, else a settled fresh one
    finally:
        source.stop()  # closes the camera only if this was the last user
    if frame is None:
        raise CameraUnavailable("the camera produced no picture")

    path = _unused_name(folder, prefix)
    try:
        save_jpeg(frame, path)
    except Exception as error:
        path.unlink(missing_ok=True)
        raise CameraUnavailable(f"could not save the picture: {error}") from error
    if not path.exists() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise CameraUnavailable("the picture file was empty")
    return path
