from dataclasses import dataclass
from typing import ClassVar, List

import pytest

from pihub.properties import StringProperty
from pihub.protocol.messages import WidgetRegistration
from pihub.widgets.base import Widget


@dataclass
class _DummyWidget(Widget):
    """A minimal concrete subclass, isolating base.Widget behaviour from
    any subclass-specific behaviour defined in output/command/presentation."""

    CAPABILITIES: ClassVar[List[str]] = ["dummy"]


def test_widget_base_class_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        Widget(id="widget-001", type="Gauge")


def test_empty_id_raises():
    with pytest.raises(ValueError):
        _DummyWidget(id="", type="Gauge")


def test_empty_type_raises():
    with pytest.raises(ValueError):
        _DummyWidget(id="widget-001", type="")


def test_properties_default_to_empty_list():
    widget = _DummyWidget(id="widget-001", type="Gauge")
    assert widget.properties == []


def test_each_instance_gets_its_own_properties_list():
    a = _DummyWidget(id="a", type="Gauge")
    b = _DummyWidget(id="b", type="Gauge")

    a.properties.append(StringProperty("title"))

    assert a.properties != b.properties
    assert b.properties == []


# --- to_registration (schema generation for the registration manifest) -----


def test_to_registration_returns_widget_registration():
    widget = _DummyWidget(id="temperature", type="Gauge")

    registration = widget.to_registration()

    assert registration == WidgetRegistration(
        widget_id="temperature", type="Gauge", capabilities=["dummy"]
    )


def test_to_registration_includes_property_schema_and_defaults():
    """Registration carries the widget's property schema and defaults,
    generated from the existing property system - not hand-built here."""
    widget = _DummyWidget(
        id="temperature",
        type="Gauge",
        properties=[
            StringProperty("title", default="Temperature"),
            StringProperty("units", default="°C"),
        ],
    )

    registration = widget.to_registration()

    assert registration.widget_id == "temperature"
    assert not hasattr(registration, "properties")  # still not a raw Property list
    assert registration.property_schema == widget.property_schema()
    assert registration.default_properties == widget.property_defaults()
    assert registration.property_schema == [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"},
    ]
    assert registration.default_properties == {"title": "Temperature", "units": "°C"}


def test_to_registration_with_no_properties_has_empty_schema_and_defaults():
    widget = _DummyWidget(id="temperature", type="Gauge")

    registration = widget.to_registration()

    assert registration.property_schema == []
    assert registration.default_properties == {}


# --- property_schema / property_defaults (schema + default generation) -----


def test_property_schema_delegates_to_properties():
    widget = _DummyWidget(
        id="temperature",
        type="Gauge",
        properties=[
            StringProperty("title", default="Temperature"),
            StringProperty("units", default="°C"),
        ],
    )

    assert widget.property_schema() == [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"},
    ]


def test_property_defaults_delegates_to_properties():
    widget = _DummyWidget(
        id="temperature",
        type="Gauge",
        properties=[StringProperty("title", default="Temperature")],
    )

    assert widget.property_defaults() == {"title": "Temperature"}


def test_property_schema_and_defaults_with_no_properties():
    widget = _DummyWidget(id="temperature", type="Gauge")

    assert widget.property_schema() == []
    assert widget.property_defaults() == {}
