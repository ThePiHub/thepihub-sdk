import pytest

from pihub.properties import SelectProperty


def test_control_is_select():
    assert SelectProperty.CONTROL == "select"


def test_options_are_required():
    with pytest.raises(ValueError):
        SelectProperty("mode", options=[])


def test_valid_default_within_options_is_accepted():
    prop = SelectProperty("mode", default="auto", options=["auto", "manual"])
    assert prop.default == "auto"


def test_default_not_in_options_raises_value_error():
    with pytest.raises(ValueError):
        SelectProperty("mode", default="turbo", options=["auto", "manual"])


def test_no_default_is_allowed():
    prop = SelectProperty("mode", options=["auto", "manual"])
    assert prop.default is None


def test_to_dict_includes_options():
    prop = SelectProperty("mode", default="auto", options=["auto", "manual"])
    assert prop.to_dict() == {
        "name": "mode",
        "label": "Mode",
        "control": "select",
        "options": ["auto", "manual"],
    }


def test_to_dict_options_is_a_copy_not_the_original_list():
    options = ["auto", "manual"]
    prop = SelectProperty("mode", options=options)

    schema = prop.to_dict()
    schema["options"].append("turbo")

    assert prop.options == ["auto", "manual"]
