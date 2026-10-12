"""Widget properties: typed descriptions of a widget's editable settings.

    from pihub.properties import StringProperty

    StringProperty("title", default="Temperature")
"""

from pihub.properties.base import Property, properties_to_defaults, properties_to_schema
from pihub.properties.boolean import BooleanProperty
from pihub.properties.number import NumberProperty
from pihub.properties.select import SelectProperty
from pihub.properties.string import StringProperty

__all__ = [
    "Property",
    "StringProperty",
    "NumberProperty",
    "BooleanProperty",
    "SelectProperty",
    "properties_to_schema",
    "properties_to_defaults",
]
