"""SelectProperty, the "Select" standard property control."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from pihub.properties.base import Property


@dataclass
class SelectProperty(Property):
    """A single-choice property, rendered by the dashboard editor's "select" control."""

    CONTROL: ClassVar[str] = "select"

    default: Optional[Any] = None
    options: List[Any] = field(default_factory=list)

    def validate(self) -> None:
        if not self.options:
            raise ValueError(f"SelectProperty {self.name!r} requires at least one option")
        if self.default is not None and self.default not in self.options:
            raise ValueError(
                f"SelectProperty {self.name!r} default {self.default!r} "
                f"is not one of its options {self.options!r}"
            )

    def extra_fields(self) -> Dict[str, Any]:
        return {"options": list(self.options)}
