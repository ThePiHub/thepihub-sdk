import pytest

from pihub.properties import StringProperty


def test_control_is_text():
    assert StringProperty.CONTROL == "text"


def test_default_is_none_when_not_given():
    prop = StringProperty("title")
    assert prop.default is None


def test_default_string_is_accepted():
    prop = StringProperty("title", default="Temperature")
    assert prop.default == "Temperature"


def test_to_dict_matches_device_sdk_example():
    prop = StringProperty("title", default="Temperature")
    assert prop.to_dict() == {"name": "title", "label": "Title", "control": "text"}


def test_non_string_default_raises_type_error():
    with pytest.raises(TypeError):
        StringProperty("title", default=5)
