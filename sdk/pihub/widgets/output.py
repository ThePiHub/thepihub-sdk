"""Output widget: an output, device -> browser (capabilities `["display"]`).

A program creates one with `output()` and updates it with `.set()` or
`.set_values()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Any, ClassVar, Dict, List, Optional, Protocol, runtime_checkable

from pihub.protocol.messages import CAPABILITY_DISPLAY
from pihub.widgets.base import Widget


@runtime_checkable
class RuntimeHost(Protocol):
    """The minimal shape `Output` needs from its owning program.

    Not part of the SDK's public API surface - `Program` satisfies this
    structurally via its private `_publish_runtime()`.
    """

    def _publish_runtime(
        self, widget_id: str, *, value: Any = None, values: Optional[Dict[str, Any]] = None
    ) -> None: ...


@dataclass
class Output(Widget):
    """An output widget: publishes runtime values from device to browser."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_DISPLAY]

    _host: Optional[RuntimeHost] = field(default=None, init=False, repr=False, compare=False)
    # A live video source (`pihub.video`), for a "Video" output only. Nothing runs until a viewer asks.
    source: Optional[Any] = field(default=None, init=False, repr=False, compare=False)
    # A pull-style output (`Section.output(..., read=...)`): the section calls `read()` and publishes what it returns (None means "no value").
    # `refresh` is "static" (once, after the device starts), "live" (every `every` seconds while someone is viewing) or "always".
    read: Optional[Callable[[], Any]] = field(default=None, init=False, repr=False, compare=False)
    refresh: Optional[str] = field(default=None, init=False, repr=False, compare=False)
    every: Optional[float] = field(default=None, init=False, repr=False, compare=False)
    background: bool = field(default=False, init=False, repr=False, compare=False)  # read a slow `static` output on a thread
    next_due: float = field(default=0.0, init=False, repr=False, compare=False)

    def bind(self, host: RuntimeHost) -> None:
        """Attach the program that will actually deliver published values."""
        self._host = host

    def use(self, source: Any) -> "Output":
        """Attach a video source to a "Video" output. The SDK publishes the stream id itself; the source
        is started only when a viewer requests the camera and stopped when they stop."""
        if self.type != "Video":
            raise ValueError(f"only a 'Video' output can use a video source (this one is {self.type!r})")
        self.source = source
        return self

    @property
    def stream_id(self) -> str:
        """The MediaStream id for this output, `<programId>.<widgetId>`, matching its `streamId` runtime value."""
        program_id = getattr(self._host, "id", None)
        return f"{program_id}.{self.id}" if program_id else self.id

    def set(self, value: Any) -> None:
        """Publish a single runtime value."""
        self._require_host()._publish_runtime(self.id, value=value)

    def set_values(self, values: Dict[str, Any]) -> None:
        """Publish a multi-value runtime update."""
        self._require_host()._publish_runtime(self.id, values=values)

    def _require_host(self) -> RuntimeHost:
        if self._host is None:
            raise RuntimeError(f"output widget {self.id!r} is not attached to a program")
        return self._host
