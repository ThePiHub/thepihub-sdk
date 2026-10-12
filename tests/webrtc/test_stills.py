"""The camera is shared by counting, and a snapshot is a real picture saved by a real encoder."""

import asyncio

import pytest

pytest.importorskip("av")

from pihub.video import CameraUnavailable, TestPatternSource, VideoSource, capture_still  # noqa: E402
from pihub.video import stills  # noqa: E402


class Counting(TestPatternSource):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.opens = self.closes = self.reads = 0

    def _open(self):
        self.opens += 1
        super()._open()

    def _close(self):
        self.closes += 1
        super()._close()

    def _read(self):
        self.reads += 1
        return super()._read()


class Broken(VideoSource):
    def _open(self):
        raise CameraUnavailable("no camera attached")


def jpeg_size(path):
    from PIL import Image  # only to CHECK the file; the SDK itself does not need it

    with Image.open(path) as image:
        assert image.format == "JPEG"
        return image.size


# --- the camera is counted, so a stream and a snapshot can share it -------------------------------------------


def test_the_camera_opens_for_the_first_user_and_closes_after_the_last():
    source = Counting()
    source.start()
    source.start()
    assert source.running and source.opens == 1  # the second user shares the open camera
    source.stop()
    assert source.running and source.closes == 0  # one user is still using it
    source.stop()
    assert not source.running and source.closes == 1


def test_a_failed_start_holds_nothing_and_an_extra_stop_is_harmless():
    source = Broken()
    with pytest.raises(CameraUnavailable):
        source.start()
    assert not source.running and source._users == 0
    source.stop()
    source.stop()  # nothing to stop: no error, no negative count
    assert source._users == 0


def test_the_latest_frame_is_remembered_only_while_running_and_fresh():
    source = Counting()
    assert source.latest_frame() is None
    source.start()
    frame = source.read()
    assert source.latest_frame() is frame
    source._latest = (source._latest[0] - 10, frame)  # ten seconds old
    assert source.latest_frame() is None
    source.stop()
    assert source.latest_frame() is None


# --- capture_still ---------------------------------------------------------------------------------------------


def test_an_idle_camera_is_opened_just_long_enough_and_a_real_jpeg_is_saved(tmp_path):
    source = Counting(size=(640, 480))

    path = capture_still(source, tmp_path / "pictures", prefix="birdbox")

    assert path.parent == tmp_path / "pictures" and path.name.startswith("birdbox-") and path.suffix == ".jpg"
    assert path.stat().st_size > 0 and jpeg_size(path) == (640, 480)
    assert source.opens == 1 and source.closes == 1 and not source.running  # opened, used, closed again
    assert source.reads == stills.SETTLE_FRAMES  # let auto-exposure settle before the one that is kept


def test_a_live_stream_is_reused_and_the_camera_is_left_running(tmp_path):
    source = Counting()
    source.start()  # a viewer's video holds the camera
    source.read()

    path = capture_still(source, tmp_path)

    assert jpeg_size(path) == (640, 480)
    assert source.running and source.opens == 1 and source.closes == 0  # not reopened, and NOT stopped under the viewer
    assert source.reads == 1  # the picture came from the stream's own frame
    source.stop()


def test_a_stalled_stream_shares_the_open_camera_instead_of_stopping_it(tmp_path):
    source = Counting()
    source.start()
    source.read()
    source._latest = (source._latest[0] - 10, source._latest[1])  # the stream's last frame is old

    path = capture_still(source, tmp_path)

    assert path.stat().st_size > 0
    assert source.running and source.closes == 0  # the viewer's hold on the camera survives the snapshot
    source.stop()
    assert not source.running


def test_a_camera_that_will_not_start_raises_and_leaves_no_file(tmp_path):
    with pytest.raises(CameraUnavailable):
        capture_still(Broken(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_an_empty_file_is_never_reported_as_a_picture(tmp_path, monkeypatch):
    monkeypatch.setattr(stills, "save_jpeg", lambda frame, path: path.write_bytes(b""))
    with pytest.raises(CameraUnavailable):
        capture_still(Counting(), tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_an_encoder_failure_is_a_clear_error_and_leaves_no_file(tmp_path, monkeypatch):
    def boom(frame, path):
        path.write_bytes(b"half")
        raise RuntimeError("disk full")

    monkeypatch.setattr(stills, "save_jpeg", boom)
    with pytest.raises(CameraUnavailable) as raised:
        capture_still(Counting(), tmp_path)
    assert "disk full" in str(raised.value) and list(tmp_path.iterdir()) == []


def test_two_pictures_in_the_same_second_do_not_overwrite_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(stills.time, "strftime", lambda fmt: "20260919-120000")
    first = capture_still(Counting(), tmp_path)
    second = capture_still(Counting(), tmp_path)
    assert first != second and first.exists() and second.exists()


def test_the_folder_is_created_and_home_is_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = capture_still(Counting(), "~/deep/er/pictures")
    assert path.parent == tmp_path / "deep" / "er" / "pictures" and path.exists()
