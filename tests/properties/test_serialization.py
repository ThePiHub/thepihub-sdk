import json

from pihub.properties import (
    BooleanProperty,
    NumberProperty,
    SelectProperty,
    StringProperty,
    properties_to_defaults,
    properties_to_schema,
)


def test_to_json_returns_valid_json_matching_to_dict():
    prop = StringProperty("title", default="Temperature")

    assert json.loads(prop.to_json()) == prop.to_dict()


def test_to_json_for_number_property():
    prop = NumberProperty("brightness", default=50)

    assert json.loads(prop.to_json()) == {
        "name": "brightness",
        "label": "Brightness",
        "control": "number",
    }


def test_to_json_for_boolean_property():
    prop = BooleanProperty("armed", default=True)

    assert json.loads(prop.to_json()) == {
        "name": "armed",
        "label": "Armed",
        "control": "boolean",
    }


def test_to_json_for_select_property_includes_options():
    prop = SelectProperty("mode", default="auto", options=["auto", "manual"])

    assert json.loads(prop.to_json()) == {
        "name": "mode",
        "label": "Mode",
        "control": "select",
        "options": ["auto", "manual"],
    }


def test_to_json_never_leaks_the_default_value():
    prop = StringProperty("title", default="Temperature")

    assert "default" not in prop.to_json()
    assert "Temperature" not in prop.to_json()


def test_properties_to_schema_output_is_json_serializable():
    properties = [
        StringProperty("title", default="Temperature"),
        NumberProperty("brightness", default=50),
        BooleanProperty("armed", default=True),
        SelectProperty("mode", default="auto", options=["auto", "manual"]),
    ]

    # Should not raise - the schema list is plain data (str/int/bool/list/dict).
    serialized = json.dumps(properties_to_schema(properties))
    assert json.loads(serialized) == properties_to_schema(properties)


def test_properties_to_defaults_output_is_json_serializable():
    properties = [
        StringProperty("title", default="Temperature"),
        NumberProperty("brightness", default=50),
        BooleanProperty("armed", default=True),
        SelectProperty("mode", default="auto", options=["auto", "manual"]),
    ]

    serialized = json.dumps(properties_to_defaults(properties))
    assert json.loads(serialized) == {
        "title": "Temperature",
        "brightness": 50,
        "armed": True,
        "mode": "auto",
    }
