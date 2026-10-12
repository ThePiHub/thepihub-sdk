def test_import_device_and_program_from_pihub():
    from pihub import Device, Program

    assert Device.__name__ == "Device"
    assert Program.__name__ == "Program"


def test_import_property_classes_from_pihub():
    from pihub import (
        BooleanProperty,
        NumberProperty,
        Property,
        SelectProperty,
        StringProperty,
    )

    assert Property.__name__ == "Property"
    assert StringProperty.__name__ == "StringProperty"
    assert NumberProperty.__name__ == "NumberProperty"
    assert BooleanProperty.__name__ == "BooleanProperty"
    assert SelectProperty.__name__ == "SelectProperty"


def test_multi_import_form_from_the_task_request():
    from pihub import StringProperty, NumberProperty, SelectProperty

    assert StringProperty("title").to_dict()["control"] == "text"
    assert NumberProperty("brightness").to_dict()["control"] == "number"
    assert SelectProperty("mode", options=["a", "b"]).to_dict()["control"] == "select"


def test_property_classes_are_the_same_objects_as_the_submodule_exports():
    import pihub
    from pihub.properties import StringProperty as SubmoduleStringProperty

    assert pihub.StringProperty is SubmoduleStringProperty
