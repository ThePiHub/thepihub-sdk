"""The Files widget: lists one or more folders on the device and lets a viewer download what is in them, one file
at a time, straight to their own machine.

```python
files = program.output(id="files", type="Files")
files.add_folder("Videos", recorder.store.directory)
files.add_folder("Photos", camera.snapshot_directory)
```

Like the Camera component, it is `["display", "command"]` with one runtime value (the folders and what is in them
right now). `CommandMessage` allows exactly one of `action` or `values` (never both), so its two commands take
different shapes: `list` is a plain action (refresh and republish; the SDK republishes on its own after a delete,
so this is only for a folder something outside pihub changed), and `delete` - which needs a folder and a file
name - travels as `values={"delete": {"folder": ..., "name": ...}}`. Both are small control-plane actions and go
through the ordinary command path - relayed or direct, whichever the viewer has.

The bytes themselves never do: downloading a file opens its own peer-to-peer DataChannel (`PeerManager`,
`WebRTC_Architecture.md` > 10.1) and needs a direct connection, the same restriction live video has. This widget
only ever answers "what files exist" and "delete this one" - the transfer itself is `PeerManager`'s, reached
through `Program.resolve_file()` so the widget stays ignorant of WebRTC entirely.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from pihub.protocol.messages import CAPABILITY_COMMAND, CAPABILITY_DISPLAY, CommandMessage
from pihub.widgets.output import Output

logger = logging.getLogger(__name__)

FILES = "Files"

# The action a Files widget answers as `CommandMessage.action`; `delete` has parameters, so it travels in `values` instead (see above).
LIST = "list"
DELETE = "delete"


@dataclass
class Files(Output):
    """A folder browser: `add_folder(label, directory)` for each folder to expose. Made by `program.output(id, type="Files")`."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_DISPLAY, CAPABILITY_COMMAND]

    _folders: Dict[str, Path] = field(default_factory=dict, init=False, repr=False, compare=False)

    def add_folder(self, label: str, directory: Any) -> "Files":
        """Expose every file directly inside `directory` (no subfolders, nothing hidden) under `label`. Call
        `publish()` after your own code adds or removes a file there; a delete through this widget republishes
        itself."""
        if not label:
            raise ValueError("a folder needs a label")
        self._folders[label] = Path(directory).expanduser()
        return self

    def folder(self, label: str) -> Optional[Path]:
        """The directory registered under `label`, or None."""
        return self._folders.get(label)

    # -- the state -------------------------------------------------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        """This widget's one runtime value: every registered folder and what is in it right now."""
        return {"folders": [self._folder_state(label) for label in self._folders]}

    def _folder_state(self, label: str) -> Dict[str, Any]:
        directory = self._folders[label]
        files: List[Dict[str, Any]] = []
        if directory.is_dir():
            for path in directory.iterdir():
                if not path.is_file() or path.suffix == ".json":
                    continue  # a recording's sidecar is metadata for the recorder, not a file to offer
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files.append({"name": path.name, "size": stat.st_size, "modifiedAt": _iso(stat.st_mtime)})
            files.sort(key=lambda entry: entry["modifiedAt"])
        return {"label": label, "files": files}

    def publish(self) -> None:
        if self._host is None:
            return
        try:
            self.set(self.state())
        except Exception:
            logger.debug("files %r could not publish its state", self.id, exc_info=True)

    # -- commands ----------------------------------------------------------------------------------------------------

    def handle(self, command: CommandMessage) -> None:
        if command.action == LIST:
            self.publish()
            return
        delete = (command.values or {}).get(DELETE)
        if isinstance(delete, dict):
            self._delete(delete.get("folder"), delete.get("name"))
        elif command.action is not None:
            logger.debug("files %r: ignoring action %r", self.id, command.action)

    def _delete(self, label: Optional[str], name: Optional[str]) -> None:
        if not isinstance(label, str) or not isinstance(name, str):
            return
        path = self.resolve(label, name)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)  # a recording's sidecar, if this file has one
        except OSError:
            logger.debug("files %r: could not delete %s/%s", self.id, label, name, exc_info=True)
            return
        self.publish()

    # -- resolving a request against what this widget actually exposes ------------------------------------------------

    def resolve(self, label: str, name: str) -> Optional[Path]:
        """The real file `label`/`name` names, or None if it is not a file this widget currently exposes - blocks
        path traversal and anything outside a registered folder. `PeerManager` calls this (through
        `Program.resolve_file`) before it will open a download; `handle()` uses it for `delete` too."""
        directory = self._folders.get(label)
        if directory is None or not name or name in (".", "..") or "/" in name or "\\" in name:
            return None
        path = directory / name
        try:
            if not directory.is_dir() or path.resolve().parent != directory.resolve():
                return None
        except OSError:
            return None
        return path if path.is_file() else None


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))
