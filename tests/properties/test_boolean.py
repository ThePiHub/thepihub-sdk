import pytest

from pihub.properties import BooleanProperty


def test_control_is_boolean():
    assert BooleanProperty.CONTROL == "boolean"


def test_default_is_false_when_not_given():
    prop = BooleanProperty("armed")
    assert prop.default is False


def test_true_default_is_accepted():
    prop = BooleanProperty("armed", default=True)
    assert prop.default is True


def test_to_dict_has_boolean_control():
    prop = BooleanProperty("armed", default=True)
    assert prop.to_dict() == {"name": "armed", "label": "Armed", "control": "boolean"}


def test_non_bool_default_raises_type_error():
    with pytest.raises(TypeError):
        BooleanProperty("armed", default="yes")
