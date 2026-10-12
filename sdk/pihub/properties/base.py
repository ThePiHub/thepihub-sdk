"""Property base class.

Widgets describe their editable properties via a property schema the
dashboard editor uses to generate controls automatically:

    {
      "propertySchema": [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"}
      ]
    }

Device-side, properties are declared per widget using typed classes, e.g.:

    StringProperty("title", default="Temperature")

`Property` is the shared dataclass base for the typed subclasses
(StringProperty, NumberProperty, BooleanProperty, SelectProperty) in the
sibling modules. Each subclass fixes its own `CONTROL` - one of the
standard property controls (Text, Number, Boolean, Select, ...) - and may contribute extra schema fields (e.g.
SelectProperty's `options`).

A `propertySchema` entry never carries a default value - only
`name`/`label`/`control` (+ any control-specific fields). Defaults are
exposed separately through each property's `.default` attribute and the
`properties_to_defaults()` helper below, mirroring the separate
`"properties": {"title": "Temperature"}` instance values a widget sends.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Iterable, Optional


def _default_label(name: str) -> str:
    """Derive a human label from a property name, e.g. "cpu_usage" -> "Cpu Usage"."""
    return " ".join(word.capitalize() for word in name.replace("_", " ").split())


@dataclass
class Property:
    """Base class for a single editable widget property.

    Not instantiated directly - use one of the typed subclasses, each of
    which fixes `CONTROL` to its "Standard Property Controls" entry.
    """

    CONTROL: ClassVar[str] = ""

    name: str
    default: Any = None
    label: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self) is Property:
            raise TypeError("Property must not be instantiated directly; use a subclass")
        if not self.name:
            raise ValueError("property name is required")
        if self.label is None:
            self.label = _default_label(self.name)
        self.validate()

    def validate(self) -> None:
        """Validate `default` (and any subclass-specific fields).

        No-op by default; subclasses override to enforce their default's
        type/shape (e.g. NumberProperty rejects a non-numeric default).
        """
        return None

    def extra_fields(self) -> Dict[str, Any]:
        """Control-specific fields to fold into `to_dict()`. Empty by default."""
        return {}

    def to_dict(self) -> Dict[str, Any]:
        """This property's entry in a widget's `propertySchema` list."""
        schema: Dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "control": self.CONTROL,
        }
        schema.update(self.extra_fields())
        return schema

    def to_json(self) -> str:
        """`to_dict()`, serialized to a JSON string.

        Mirrors the `to_json()` convention already used by `Envelope` and
        the protocol message classes.
        """
        return json.dumps(self.to_dict())


def properties_to_schema(properties: Iterable[Property]) -> list:
    """Build a widget's full `propertySchema` list, for the dashboard editor."""
    return [prop.to_dict() for prop in properties]


def properties_to_defaults(properties: Iterable[Property]) -> Dict[str, Any]:
    """Build a widget's default `properties` values, e.g. `{"title": "Temperature"}`."""
    return {prop.name: prop.default for prop in properties}
