"""Command widget: an input, browser -> device (capabilities `["command"]`).

A program creates one with `command()` and attaches a handler with `@handler`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, ClassVar, List, Optional, Protocol, runtime_checkable

from pihub.protocol.messages import CAPABILITY_COMMAND, CommandMessage
from pihub.widgets.base import Widget

CommandHandler = Callable[[CommandMessage], None]


@runtime_checkable
class CommandHost(Protocol):
    """The minimal shape `Command` needs from its owning program.

    Not part of the SDK's public API surface - `Program` satisfies this
    structurally via its private `_register_command_handler()`.
    """

    def _register_command_handler(self, widget_id: str, handler: CommandHandler) -> None: ...


@dataclass
class Command(Widget):
    """A command widget: receives actions from the browser."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_COMMAND]

    _host: Optional[CommandHost] = field(default=None, init=False, repr=False, compare=False)

    def bind(self, host: CommandHost) -> None:
        """Attach the program that will register this widget's handler."""
        self._host = host

    def handler(self, func: CommandHandler) -> CommandHandler:
        """Register `func` to run when this widget's command arrives.

        Used as a bare decorator: `@snapshot.handler`.
        """
        if self._host is None:
            raise RuntimeError(f"command widget {self.id!r} is not attached to a program")
        self._host._register_command_handler(self.id, func)
        return func
