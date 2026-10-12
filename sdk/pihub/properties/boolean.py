"""BooleanProperty, the "Boolean" standard property control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pihub.properties.base import Property


@dataclass
class BooleanProperty(Property):
    """A true/false property, rendered by the dashboard editor's "boolean" control."""

    CONTROL: ClassVar[str] = "boolean"

    default: bool = False

    def validate(self) -> None:
        if not isinstance(self.default, bool):
            raise TypeError(
                f"BooleanProperty {self.name!r} default must be a bool, "
                f"got {type(self.default).__name__}"
            )
