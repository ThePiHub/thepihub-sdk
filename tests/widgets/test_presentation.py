import pytest

from pihub.protocol.messages import CAPABILITY_LAYOUT, WidgetRegistration
from pihub.widgets.presentation import Presentation


def test_capabilities_is_layout_only():
    assert Presentation.CAPABILITIES == [CAPABILITY_LAYOUT]


def test_to_registration_matches_widget_spec_presentation_capabilities():
    heading = Presentation(id="hdr", type="Heading")

    assert heading.to_registration() == WidgetRegistration(
        widget_id="hdr", type="Heading", capabilities=["layout"]
    )


def test_empty_id_raises():
    with pytest.raises(ValueError):
        Presentation(id="", type="Heading")


def test_presentation_has_no_runtime_behaviour():
    # Per "Presentation Widgets": "never communicate with devices",
    # "No Runtime State, No Device Binding" - unlike Output/Command,
    # there is no bind()/set()/set_values()/handler().
    heading = Presentation(id="hdr", type="Heading")

    assert not hasattr(heading, "bind")
    assert not hasattr(heading, "set")
    assert not hasattr(heading, "set_values")
    assert not hasattr(heading, "handler")


def test_property_schema_and_defaults_still_work_for_presentation():
    from pihub.properties import StringProperty

    folder = Presentation(id="folder-1", type="Folder", properties=[StringProperty("title", default="Sensors")])

    assert folder.property_schema() == [{"name": "title", "label": "Title", "control": "text"}]
    assert folder.property_defaults() == {"title": "Sensors"}
