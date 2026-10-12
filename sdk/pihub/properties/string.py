"""StringProperty, the "Text" standard property control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Optional

from pihub.properties.base import Property


@dataclass
class StringProperty(Property):
    """A free-text property, rendered by the dashboard editor's "text" control."""

    CONTROL: ClassVar[str] = "text"

    default: Optional[str] = None
    # An optional cap, for a Text setting. Appears in the schema only when set, so every existing StringProperty registers exactly as before.
    max_length: Optional[int] = None

    def validate(self) -> None:
        if self.default is not None and not isinstance(self.default, str):
            raise TypeError(
                f"StringProperty {self.name!r} default must be a str, "
                f"got {type(self.default).__name__}"
            )
        if self.max_length is not None and self.max_length <= 0:
            raise ValueError(f"StringProperty {self.name!r} max_length must be positive")

    def extra_fields(self) -> Dict[str, Any]:
        return {"maxLength": self.max_length} if self.max_length is not None else {}
