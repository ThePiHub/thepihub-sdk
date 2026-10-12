"""The Files widget: folder listings, the `list`/`delete` commands, and `resolve()` (what `PeerManager` uses,
through `Program.resolve_file()`/`Device._resolve_file()`, to answer a download). The transfer itself is
`PeerManager`'s and is covered in `tests/webrtc/test_peer_manager.py`, not here.
"""

from __future__ import annotations

import json

from pihub import Device, Program
from pihub.protocol.bindings import Binding
from pihub.protocol.messages import CommandMessage

DEVICE, PROGRAM = "cam-1", "birdbox"


class Transport:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, data: str) -> None:
        self.sent.append(data)

    def states(self, widget: str) -> list:
        found = []
        for raw in self.sent:
            payload = json.loads(raw).get("payload", {})
            if payload.get("binding", {}).get("widgetId") == widget and "value" in payload:
                found.append(payload["value"])
        return found


def _command_json(*, program_id: str, widget_id: str, **kwargs) -> str:
    binding = Binding(device_id=DEVICE, program_id=program_id, widget_id=widget_id)
    return CommandMessage(binding=binding, **kwargs).to_json()


def _make(tmp_path):
    transport = Transport()
    device = Device(id=DEVICE, transport=transport, device_information=False)
    program = Program(id=PROGRAM, name="Bird Box")
    files = program.output(id="files", type="Files")
    videos, photos = tmp_path / "videos", tmp_path / "photos"
    videos.mkdir()
    photos.mkdir()
    files.add_folder("Videos", videos)
    files.add_folder("Photos", photos)
    device.add_program(program)
    device.start()
    return device, transport, files, videos, photos


def test_files_widget_is_bidirectional_with_no_binding_shown():
    program = Program(id=PROGRAM, name="Bird Box")
    files = program.output(id="files", type="Files")
    registration = files.to_registration()
    assert registration.capabilities == ["display", "command"]


def test_state_lists_every_file_in_every_registered_folder(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    (videos / "rec-1-00001-20260101-000000.mp4").write_bytes(b"abc")
    (videos / "rec-1-00001-20260101-000000.json").write_text("{}")  # a sidecar: never listed
    (photos / "cam-1-20260101-000000.jpg").write_bytes(b"jpeg")

    files.publish()

    state = transport.states("files")[-1]
    by_label = {folder["label"]: folder["files"] for folder in state["folders"]}
    assert [f["name"] for f in by_label["Videos"]] == ["rec-1-00001-20260101-000000.mp4"]
    assert by_label["Videos"][0]["size"] == 3
    assert [f["name"] for f in by_label["Photos"]] == ["cam-1-20260101-000000.jpg"]


def test_state_folder_with_nothing_written_yet_is_empty(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    files.publish()
    state = transport.states("files")[-1]
    assert all(folder["files"] == [] for folder in state["folders"])


def test_list_action_republishes(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    (videos / "a.mp4").write_bytes(b"x")
    before = len(transport.states("files"))

    device.handle_incoming(_command_json(program_id=PROGRAM, widget_id="files", action="list"))

    after = transport.states("files")
    assert len(after) == before + 1
    assert after[-1]["folders"][0]["files"][0]["name"] == "a.mp4"


def test_delete_action_removes_the_file_and_republishes(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    (videos / "rec-1-00001-20260101-000000.mp4").write_bytes(b"abc")
    (videos / "rec-1-00001-20260101-000000.json").write_text("{}")

    device.handle_incoming(
        _command_json(
            program_id=PROGRAM, widget_id="files",
            values={"delete": {"folder": "Videos", "name": "rec-1-00001-20260101-000000.mp4"}},
        )
    )

    assert not (videos / "rec-1-00001-20260101-000000.mp4").exists()
    assert not (videos / "rec-1-00001-20260101-000000.json").exists()
    state = transport.states("files")[-1]
    assert state["folders"][0]["files"] == []


def test_delete_action_with_an_unknown_file_does_nothing(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    before = len(transport.states("files"))

    device.handle_incoming(
        _command_json(program_id=PROGRAM, widget_id="files", values={"delete": {"folder": "Videos", "name": "missing.mp4"}})
    )

    assert len(transport.states("files")) == before  # nothing to publish: nothing changed


def test_resolve_blocks_path_traversal(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    (tmp_path / "secret.txt").write_text("nope")

    assert files.resolve("Videos", "../secret.txt") is None
    assert files.resolve("Videos", "..") is None
    assert files.resolve("Videos", "/etc/passwd") is None
    assert files.resolve("Unknown Folder", "a.mp4") is None


def test_resolve_returns_the_real_path_for_a_file_it_lists(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    target = videos / "a.mp4"
    target.write_bytes(b"abc")

    assert files.resolve("Videos", "a.mp4") == target


def test_resolve_refuses_a_file_outside_the_registered_folder(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    (photos / "a.mp4").write_bytes(b"abc")  # same name, but in the OTHER registered folder

    assert files.resolve("Videos", "a.mp4") is None


def test_program_resolve_file_delegates_to_the_files_widget(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    target = videos / "a.mp4"
    target.write_bytes(b"abc")
    program = device._programs[PROGRAM]

    assert program.resolve_file("files", "Videos", "a.mp4") == target
    assert program.resolve_file("files", "Videos", "missing.mp4") is None
    assert program.resolve_file("not-a-widget", "Videos", "a.mp4") is None


def test_device_resolve_file_delegates_to_the_right_program(tmp_path):
    device, transport, files, videos, photos = _make(tmp_path)
    target = videos / "a.mp4"
    target.write_bytes(b"abc")

    assert device._resolve_file(PROGRAM, "files", "Videos", "a.mp4") == target
    assert device._resolve_file("not-a-program", "files", "Videos", "a.mp4") is None


def test_non_files_widget_answers_no_resolve(tmp_path):
    transport = Transport()
    device = Device(id=DEVICE, transport=transport, device_information=False)
    program = Program(id=PROGRAM, name="Bird Box")
    program.output(id="temperature", type="Gauge")
    device.add_program(program)
    device.start()

    assert program.resolve_file("temperature", "Videos", "a.mp4") is None
