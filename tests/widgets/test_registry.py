import dataclasses
import re
from pathlib import Path

import pytest

from pihub.widgets.registry import (
    CATEGORY_BIDIRECTIONAL,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_PRESENTATION,
    WidgetRegistry,
    WidgetTypeInfo,
    capabilities_for,
)


@pytest.mark.parametrize(
    "type_name, category, capabilities",
    [
        ("Gauge", CATEGORY_OUTPUT, ["display"]),
        ("Chart", CATEGORY_OUTPUT, ["display"]),
        ("Status", CATEGORY_OUTPUT, ["display"]),
        ("Video", CATEGORY_OUTPUT, ["display"]),
        ("Button", CATEGORY_INPUT, ["command"]),
        ("Toggle", CATEGORY_BIDIRECTIONAL, ["display", "command"]),  # a setting: it shows a value and changes it
        ("Select", CATEGORY_BIDIRECTIONAL, ["display", "command"]),
        ("Slider", CATEGORY_BIDIRECTIONAL, ["display", "command"]),
        ("Joystick", CATEGORY_INPUT, ["command"]),
        ("Text", CATEGORY_BIDIRECTIONAL, ["display", "command"]),
        ("Camera", CATEGORY_BIDIRECTIONAL, ["display", "command"]),
        ("Files", CATEGORY_BIDIRECTIONAL, ["display", "command"]),
        ("Heading", CATEGORY_PRESENTATION, ["layout"]),
        ("Folder", CATEGORY_PRESENTATION, ["layout"]),
        ("Tabs", CATEGORY_PRESENTATION, ["layout"]),
        ("Markdown", CATEGORY_PRESENTATION, ["layout"]),
    ],
)
def test_get_matches_built_in_widgets_table(type_name, category, capabilities):
    info = WidgetRegistry.get(type_name)
    assert info.type == type_name
    assert info.category == category
    assert info.capabilities == capabilities


def test_get_unknown_type_raises_key_error():
    with pytest.raises(KeyError):
        WidgetRegistry.get("FlightTracker")


def test_is_known_true_for_built_in_type():
    assert WidgetRegistry.is_known("Gauge") is True


def test_is_known_false_for_unknown_type():
    assert WidgetRegistry.is_known("FlightTracker") is False


def test_all_returns_every_built_in_type_exactly_once():
    all_types = [info.type for info in WidgetRegistry.all()]
    assert len(all_types) == len(set(all_types))
    assert set(all_types) == {
        "Gauge",
        "Chart",
        "Status",
        "Video",
        "Button",
        "Toggle",
        "Select",
        "Slider",
        "Joystick",
        "Text",
        "Camera",
        "Files",
        "Heading",
        "Folder",
        "Tabs",
        "Markdown",
    }


def test_capabilities_for_matches_get():
    assert capabilities_for("Chart") == ["display"]


def test_capabilities_for_unknown_type_raises_key_error():
    with pytest.raises(KeyError):
        capabilities_for("FlightTracker")


def test_widget_type_info_is_frozen():
    info = WidgetRegistry.get("Gauge")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.type = "Chart"


def test_capabilities_is_a_copy_not_shared_state():
    info = WidgetRegistry.get("Gauge")

    capabilities = info.capabilities
    capabilities.append("command")

    assert WidgetRegistry.get("Gauge").capabilities == ["display"]


def test_widget_type_info_can_be_constructed_directly():
    info = WidgetTypeInfo(type="Custom", category=CATEGORY_OUTPUT)
    assert info.capabilities == ["display"]


# -- what the dashboard can draw (the catalog must not drift from the frontend again) ------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]
FRONTEND_REGISTRY = REPO / "frontend" / "src" / "widgets" / "registry.ts"

RESERVED = {"Joystick", "Folder", "Tabs", "Markdown"}  # names the SDK accepts that the dashboard has no component for yet


def test_the_types_with_no_dashboard_component_are_marked_and_the_rest_are_not():
    reserved = {info.type for info in WidgetRegistry.all() if not info.drawn}
    assert reserved == RESERVED


def test_a_reserved_type_is_still_a_known_type():
    """Marking a type as not drawn must not make the SDK stop accepting its name."""
    for name in RESERVED:
        assert WidgetRegistry.is_known(name)


@pytest.mark.skipif(not FRONTEND_REGISTRY.is_file(), reason="no dashboard source in this copy of the repository")
def test_the_drawn_types_are_exactly_the_types_the_dashboard_has_a_component_for():
    source = FRONTEND_REGISTRY.read_text()
    body = re.search(r"WIDGET_COMPONENTS[^=]*=\s*\{(.*?)\n\}", source, re.S)
    assert body, "could not find WIDGET_COMPONENTS in the frontend widget registry: update this test to match"
    in_the_dashboard = set(re.findall(r"^\s+(\w+):", body.group(1), re.M))
    assert len(in_the_dashboard) >= 10, f"read too few widget types from the frontend registry: {in_the_dashboard}"
    drawn = {info.type for info in WidgetRegistry.all() if info.drawn}
    assert drawn == in_the_dashboard, f"in the SDK catalog only: {drawn - in_the_dashboard}; in the dashboard only: {in_the_dashboard - drawn}"
