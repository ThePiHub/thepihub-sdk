from typing import List, Tuple

import pytest

from pihub.properties import BooleanProperty
from pihub.protocol.messages import CAPABILITY_COMMAND, WidgetRegistration
from pihub.widgets.command import Command, CommandHandler


class FakeHost:
    """Minimal stand-in for a Program, satisfying `CommandHost` structurally."""

    def __init__(self) -> None:
        self.registered: List[Tuple[str, CommandHandler]] = []

    def _register_command_handler(self, widget_id: str, handler: CommandHandler) -> None:
        self.registered.append((widget_id, handler))


# --- schema generation --------------------------------------------------


def test_capabilities_is_command_only():
    assert Command.CAPABILITIES == [CAPABILITY_COMMAND]


def test_to_registration_matches_widget_spec_input_capabilities():
    command = Command(id="snapshot", type="Button")

    assert command.to_registration() == WidgetRegistration(
        widget_id="snapshot", type="Button", capabilities=["command"]
    )


def test_property_schema_uses_existing_property_system():
    command = Command(id="alarm", type="Toggle", properties=[BooleanProperty("armed", default=False)])

    assert command.property_schema() == [{"name": "armed", "label": "Armed", "control": "boolean"}]


# --- default generation ----------------------------------------------------


def test_property_defaults_uses_existing_property_system():
    command = Command(id="alarm", type="Toggle", properties=[BooleanProperty("armed", default=True)])

    assert command.property_defaults() == {"armed": True}


# --- validation --------------------------------------------------------------


def test_empty_id_raises():
    with pytest.raises(ValueError):
        Command(id="", type="Button")


def test_handler_before_bind_raises():
    command = Command(id="snapshot", type="Button")

    def take_snapshot(command_message):
        pass

    with pytest.raises(RuntimeError):
        command.handler(take_snapshot)


# --- handler registration ----------------------------------------------------


def test_handler_registers_on_bound_host_and_returns_original_function():
    command = Command(id="snapshot", type="Button")
    host = FakeHost()
    command.bind(host)

    def take_snapshot(command_message):
        pass

    result = command.handler(take_snapshot)

    assert result is take_snapshot
    assert host.registered == [("snapshot", take_snapshot)]


def test_handler_used_as_bare_decorator():
    command = Command(id="snapshot", type="Button")
    host = FakeHost()
    command.bind(host)

    @command.handler
    def take_snapshot(command_message):
        pass

    assert host.registered == [("snapshot", take_snapshot)]
