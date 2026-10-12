from typing import Any, Dict, List, Optional

import pytest

from pihub.properties import NumberProperty, StringProperty
from pihub.protocol.messages import CAPABILITY_DISPLAY, WidgetRegistration
from pihub.widgets.output import Output


class FakeHost:
    """Minimal stand-in for a Program, satisfying `RuntimeHost` structurally."""

    def __init__(self) -> None:
        self.calls: List[Any] = []

    def _publish_runtime(
        self, widget_id: str, *, value: Any = None, values: Optional[Dict[str, Any]] = None
    ) -> None:
        self.calls.append((widget_id, value, values))


# --- schema generation --------------------------------------------------


def test_capabilities_is_display_only():
    assert Output.CAPABILITIES == [CAPABILITY_DISPLAY]


def test_to_registration_matches_widget_spec_output_capabilities():
    output = Output(id="temperature", type="Gauge")

    assert output.to_registration() == WidgetRegistration(
        widget_id="temperature", type="Gauge", capabilities=["display"]
    )


def test_property_schema_uses_existing_property_system():
    output = Output(
        id="temperature",
        type="Gauge",
        properties=[
            StringProperty("title", default="Temperature"),
            StringProperty("units", default="°C"),
        ],
    )

    assert output.property_schema() == [
        {"name": "title", "label": "Title", "control": "text"},
        {"name": "units", "label": "Units", "control": "text"},
    ]


# --- default generation ----------------------------------------------------


def test_property_defaults_uses_existing_property_system():
    output = Output(id="brightness", type="Slider", properties=[NumberProperty("max", default=100)])

    assert output.property_defaults() == {"max": 100}


def test_properties_default_to_empty_list():
    output = Output(id="temperature", type="Gauge")
    assert output.properties == []
    assert output.property_schema() == []
    assert output.property_defaults() == {}


# --- validation --------------------------------------------------------------


def test_empty_id_raises():
    with pytest.raises(ValueError):
        Output(id="", type="Gauge")


def test_set_before_bind_raises():
    output = Output(id="temperature", type="Gauge")
    with pytest.raises(RuntimeError):
        output.set(22.5)


def test_set_values_before_bind_raises():
    output = Output(id="system", type="SystemHealth")
    with pytest.raises(RuntimeError):
        output.set_values({"cpu": 12})


# --- serialization / delivery: bind() + set()/set_values() ------------------


def test_set_delivers_single_value_through_bound_host():
    output = Output(id="temperature", type="Gauge")
    host = FakeHost()
    output.bind(host)

    output.set(22.5)

    assert host.calls == [("temperature", 22.5, None)]


def test_set_values_delivers_multi_value_through_bound_host():
    output = Output(id="system", type="SystemHealth")
    host = FakeHost()
    output.bind(host)

    output.set_values({"cpu": 12, "memory": 40, "disk": 75})

    assert host.calls == [("system", None, {"cpu": 12, "memory": 40, "disk": 75})]


def test_rebinding_to_a_new_host_redirects_future_sends():
    output = Output(id="temperature", type="Gauge")
    first_host = FakeHost()
    second_host = FakeHost()

    output.bind(first_host)
    output.bind(second_host)
    output.set(1.0)

    assert first_host.calls == []
    assert second_host.calls == [("temperature", 1.0, None)]
