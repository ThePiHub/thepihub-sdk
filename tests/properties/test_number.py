import pytest

from pihub.properties import NumberProperty


def test_control_is_number():
    assert NumberProperty.CONTROL == "number"


def test_default_is_none_when_not_given():
    prop = NumberProperty("brightness")
    assert prop.default is None


def test_int_default_is_accepted():
    prop = NumberProperty("brightness", default=50)
    assert prop.default == 50


def test_float_default_is_accepted():
    prop = NumberProperty("threshold", default=3.14)
    assert prop.default == 3.14


def test_to_dict_has_number_control():
    prop = NumberProperty("brightness", default=50)
    assert prop.to_dict() == {"name": "brightness", "label": "Brightness", "control": "number"}


def test_bool_default_is_rejected():
    # bool is a subclass of int - must not be silently accepted as numeric.
    with pytest.raises(TypeError):
        NumberProperty("armed", default=True)


def test_non_numeric_default_raises_type_error():
    with pytest.raises(TypeError):
        NumberProperty("brightness", default="fifty")
