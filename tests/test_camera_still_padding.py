"""A snapshot from the Pi's hardware-encoder source has no padding in it.

The Pi's image processor pads each row of a picture to a multiple of 64 bytes, so at 800x600 `capture_array("main")` returns rows 832 wide with the last 32 columns zero. Read as a picture,
all-zero is solid green: every snapshot at that size had a green strip down its right edge (a Pi Zero, 2026-10-08; the strip's pixels were exactly (0, 135, 0), what an all-zero pixel is).
`crop_yuv420` takes the padding off, plane by plane (a YUV420 array is luma, then U, then V, each chroma plane packed two lines to a row).

No Pi here: the arrays are made by hand with the padding the camera adds, with every real pixel non-zero, so any zero that reaches the picture is padding. A real Pi is the only thing that
shows the camera's own padding (the end-to-end check was made on a real Pi Zero, 2026-10-08).
"""

from __future__ import annotations

import logging
import types
from typing import Tuple

import numpy as np
import pytest

from pihub.video import HardwareCameraSource, capture_still, hardware, sources
from pihub.video.hardware import crop_yuv420
from tests.test_camera_orientation import FakeEncoder, FakeOutput, FakePicamera2

WIDTH, HEIGHT, STRIDE = 80, 60, 128  # 128 is 80 padded up to a multiple of 64, as 800 is to 832


def planes(width: int = WIDTH, height: int = HEIGHT) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Luma, U and V with no zero in them, each value depending on its row and column so a plane that is shifted or swapped shows."""
    rows, cols = np.indices((height, width))
    luma = ((rows * 3 + cols) % 200 + 20).astype("uint8")
    rows, cols = np.indices((height // 2, width // 2))
    u = ((rows * 5 + cols * 2) % 150 + 40).astype("uint8")
    v = ((rows * 7 + cols * 3) % 120 + 60).astype("uint8")
    return luma, u, v


def padded(stride: int = STRIDE, width: int = WIDTH, height: int = HEIGHT) -> np.ndarray:
    """What the camera hands over: each plane's lines padded with zeros to `stride` (half of it for chroma), the chroma planes packed two lines to a row."""
    luma, u, v = planes(width, height)

    def pad(plane: np.ndarray, to: int) -> np.ndarray:
        out = np.zeros((plane.shape[0], to), dtype="uint8")
        out[:, : plane.shape[1]] = plane
        return out

    return np.concatenate([pad(luma, stride), pad(u, stride // 2).reshape(height // 4, stride), pad(v, stride // 2).reshape(height // 4, stride)])


def planes_of(frame) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The picture's three planes as an `av.VideoFrame` holds them, without the frame's own row padding."""
    out = []
    for plane, width, height in zip(frame.planes, (frame.width, frame.width // 2, frame.width // 2), (frame.height, frame.height // 2, frame.height // 2)):
        out.append(np.frombuffer(bytes(plane), dtype="uint8").reshape(-1, plane.line_size)[:height, :width])
    return tuple(out)  # type: ignore[return-value]


def test_the_camera_pads_the_rows_and_the_helper_takes_it_off():
    av = pytest.importorskip("av")
    array = padded()
    assert array.shape == (HEIGHT * 3 // 2, STRIDE)
    assert av.VideoFrame.from_ndarray(array, format="yuv420p").width == STRIDE  # the bug: a picture 128 wide, 48 columns of it zero

    frame = av.VideoFrame.from_ndarray(crop_yuv420(array, WIDTH, HEIGHT), format="yuv420p")

    assert (frame.width, frame.height) == (WIDTH, HEIGHT)
    for got, want in zip(planes_of(frame), planes()):
        assert (got == want).all()  # the real picture, every pixel, in the right plane
        assert got.min() > 0  # and no padding left in it


def test_a_picture_with_no_padding_is_returned_as_it_is():
    array = padded(stride=WIDTH)
    assert crop_yuv420(array, WIDTH, HEIGHT) is array


def test_a_layout_it_does_not_know_is_left_alone_with_a_warning(caplog):
    odd_height = np.zeros((62 * 3 // 2, STRIDE), dtype="uint8")  # 62 is not a multiple of 4: the chroma would not pack into whole rows
    with caplog.at_level(logging.WARNING, logger=hardware.logger.name):
        assert crop_yuv420(odd_height, WIDTH, 62) is odd_height
    assert "unexpected shape" in caplog.text

    narrow = np.zeros((HEIGHT * 3 // 2, WIDTH - 2), dtype="uint8")  # narrower than the size the source was told: something else is wrong
    assert crop_yuv420(narrow, WIDTH, HEIGHT) is narrow


def test_the_sizes_the_dashboard_offers_are_cropped_to_exactly_their_size():
    """800x600 is padded (to 832); the others are not, and none of them may change size or lose a pixel."""
    av = pytest.importorskip("av")
    for width, height in sources.RESOLUTIONS:
        stride = -(-width // 64) * 64
        frame = av.VideoFrame.from_ndarray(crop_yuv420(padded(stride, width, height), width, height), format="yuv420p")
        assert (frame.width, frame.height) == (width, height)
        for got, want in zip(planes_of(frame), planes(width, height)):
            assert (got == want).all(), (width, height)


class PaddedCamera(FakePicamera2):
    """The orientation tests' fake camera, which answers `capture_array("main")` with a padded picture, as the Pi's does at this size."""

    def capture_array(self, name):
        return padded()


@pytest.fixture
def source(monkeypatch):
    library = types.SimpleNamespace(Picamera2=PaddedCamera, H264Encoder=FakeEncoder, LibavH264Encoder=FakeEncoder, Output=FakeOutput)
    monkeypatch.setattr(sources, "import_picamera2", lambda: library)
    monkeypatch.setattr(hardware, "import_picamera2", lambda: library)
    made = HardwareCameraSource(size=(WIDTH, HEIGHT), fps=15, tuning="")
    yield made
    while made.running:
        made.stop()


def test_a_snapshot_from_the_source_is_the_size_asked_for_with_no_padding_in_it(source, tmp_path):
    pytest.importorskip("av")
    source.start()
    try:
        frame = source.grab_still()
    finally:
        source.stop()

    assert (frame.width, frame.height) == (WIDTH, HEIGHT)
    assert all(plane.min() > 0 for plane in planes_of(frame))


def test_the_saved_file_is_the_size_asked_for(source, tmp_path):
    av = pytest.importorskip("av")
    path = capture_still(source, tmp_path)

    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        assert (stream.width, stream.height) == (WIDTH, HEIGHT)
