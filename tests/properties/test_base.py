from dataclasses import dataclass
from typing import ClassVar

import pytest

from pihub.properties.base import Property, properties_to_defaults, properties_to_schema


@dataclass
class _DummyProperty(Property):
    """A minimal concrete subclass, isolating base.Property behaviour from
    any subclass-specific validation defined in string/number/boolean/select."""

    CONTROL: ClassVar[str] = "dummy"


def test_property_base_class_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Property(name="title")


def test_empty_name_raises():
    with pytest.raises(ValueError):
        _DummyProperty(name="")


def test_label_is_auto_derived_from_name():
    prop = _DummyProperty(name="title")
    assert prop.label == "Title"


def test_label_auto_derivation_replaces_underscores():
    prop = _DummyProperty(name="cpu_usage")
    assert prop.label == "Cpu Usage"


def test_explicit_label_is_not_overridden():
    prop = _DummyProperty(name="title", label="Custom Label")
    assert prop.label == "Custom Label"


def test_default_defaults_to_none():
    prop = _DummyProperty(name="title")
    assert prop.default is None


def test_to_dict_contains_name_label_control_and_no_default():
    prop = _DummyProperty(name="title", default="Temperature")
    schema = prop.to_dict()
    assert schema == {"name": "title", "label": "Title", "control": "dummy"}
    assert "default" not in schema


def test_extra_fields_defaults_to_empty():
    prop = _DummyProperty(name="title")
    assert prop.extra_fields() == {}


# --- module-level helpers, mirroring the spec's JSON examples --------------


def test_properties_to_schema_matches_widget_spec_example():
    from pihub.properties.string import StringProperty

    properties = [
        StringProperty("title", default="Temperature"),
        StringProperty("units", default="°C"),
    ]

    assert properties_to_schema(properties) == [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"},
    ]


def test_properties_to_defaults_matches_widget_spec_example():
    from pihub.properties.string import StringProperty

    properties = [
        StringProperty("title", default="Temperature"),
        StringProperty("units", default="°C"),
    ]

    assert properties_to_defaults(properties) == {"title": "Temperature", "units": "°C"}


def test_properties_to_schema_with_no_properties_is_empty():
    assert properties_to_schema([]) == []


def test_properties_to_defaults_with_no_properties_is_empty():
    assert properties_to_defaults([]) == {}
