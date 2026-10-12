"""Widget base class.

Widgets represent runtime endpoints - data sources and command endpoints; the
platform decides how they are rendered.

This models the *device SDK's* widget: a runtime endpoint a `Program`
exposes (Output or Command) - not the platform's broader dashboard-widget
concept (which additionally carries a dashboard-instance id, a `binding`
back to the device endpoint, and layout, and is owned by the platform). See
`pihub/widgets/__init__.py` for the full boundary this package draws.

Every widget carries the properties it declares (`pihub.properties`), and
`to_registration()` folds `property_schema()` / `property_defaults()`
into the widget's registration entry - the existing property system
(`Property.to_dict()`, `properties_to_schema()`, `properties_to_defaults()`)
is the single source of truth for that data; this class does not
duplicate or hardcode its shape. A widget declared with no properties
produces the same registration entry as one from an SDK that predates
properties - see `WidgetRegistration`'s own docstring for how the wire
format stays backward compatible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List

from pihub.properties.base import Property, properties_to_defaults, properties_to_schema
from pihub.protocol.messages import WidgetRegistration


@dataclass
class Widget:
    """Base for the SDK's runtime-endpoint widgets.

    Not instantiated directly - use a subclass (Output, Command,
    Presentation), each of which fixes `CAPABILITIES` per "Widget
    Categories".
    """

    CAPABILITIES: ClassVar[List[str]] = []

    id: str
    type: str
    properties: List[Property] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.__class__ is Widget:
            raise TypeError("Widget must not be instantiated directly; use a subclass")
        if not self.id:
            raise ValueError("widget id is required")
        if not self.type:
            raise ValueError("widget type is required")

    def to_registration(self) -> WidgetRegistration:
        """This widget's entry in a program's registration manifest,
        including its property schema and default values (if it declares
        any properties at all)."""
        return WidgetRegistration(
            widget_id=self.id,
            type=self.type,
            capabilities=list(self.CAPABILITIES),
            property_schema=self.property_schema(),
            default_properties=self.property_defaults(),
        )

    def property_schema(self) -> List[Dict[str, Any]]:
        """This widget's `propertySchema`, per "Widget Properties"."""
        return properties_to_schema(self.properties)

    def property_defaults(self) -> Dict[str, Any]:
        """This widget's default `properties` values, per "Widget Properties"."""
        return properties_to_defaults(self.properties)
