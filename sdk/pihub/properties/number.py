"""NumberProperty, the "Number" standard property control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, ClassVar, Optional, Union

from pihub.properties.base import Property


@dataclass
class NumberProperty(Property):
    """A numeric property, rendered by the dashboard editor's "number" control."""

    CONTROL: ClassVar[str] = "number"

    default: Optional[Union[int, float]] = None
    # Optional bounds, for a Slider. They appear in the schema only when set, so every existing NumberProperty registers exactly as before.
    minimum: Optional[Union[int, float]] = None
    maximum: Optional[Union[int, float]] = None
    step: Optional[Union[int, float]] = None

    def validate(self) -> None:
        for label, value in (("minimum", self.minimum), ("maximum", self.maximum), ("step", self.step)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise TypeError(f"NumberProperty {self.name!r} {label} must be a number, got {type(value).__name__}")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"NumberProperty {self.name!r} minimum {self.minimum} is above its maximum {self.maximum}")
        if self.step is not None and self.step <= 0:
            raise ValueError(f"NumberProperty {self.name!r} step must be positive")
        if self.default is None:
            return
        if isinstance(self.default, bool) or not isinstance(self.default, (int, float)):
            raise TypeError(
                f"NumberProperty {self.name!r} default must be a number, "
                f"got {type(self.default).__name__}"
            )
        if (self.minimum is not None and self.default < self.minimum) or (self.maximum is not None and self.default > self.maximum):
            raise ValueError(f"NumberProperty {self.name!r} default {self.default} is outside its range")

    def extra_fields(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in (("min", self.minimum), ("max", self.maximum), ("step", self.step))
            if value is not None
        }
