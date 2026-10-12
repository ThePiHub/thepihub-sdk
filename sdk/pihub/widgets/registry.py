"""Widget type registry.

A dashboard's widget registry (used by the renderer, toolbox and property
editor) pairs each widget type with a UI component. That is a platform
concept - this Python device SDK has no UI components to register.

What the device SDK can usefully offer is the *data* half of that
registry: the catalog of built-in widget *types*, each tagged with its
category and default capabilities. This is a lookup table only - it is not
wired into `Output`/`Command` as enforced validation, since composite,
marketplace and custom widgets are legitimately outside this built-in
catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from pihub.protocol.messages import CAPABILITY_COMMAND, CAPABILITY_DISPLAY, CAPABILITY_LAYOUT

CATEGORY_OUTPUT = "output"
CATEGORY_INPUT = "input"
CATEGORY_BIDIRECTIONAL = "bidirectional"
CATEGORY_PRESENTATION = "presentation"

_CAPABILITIES_BY_CATEGORY: Dict[str, List[str]] = {
    CATEGORY_OUTPUT: [CAPABILITY_DISPLAY],
    CATEGORY_INPUT: [CAPABILITY_COMMAND],
    CATEGORY_BIDIRECTIONAL: [CAPABILITY_DISPLAY, CAPABILITY_COMMAND],
    CATEGORY_PRESENTATION: [CAPABILITY_LAYOUT],
}


@dataclass(frozen=True)
class WidgetTypeInfo:
    """A single built-in widget catalog entry.

    `drawn` says whether the dashboard has a component for this type today. A type with `drawn=False` is a reserved name: the SDK accepts a component
    of that type, but the dashboard draws nothing for it (a test compares this catalog with the dashboard's list of drawn types).
    """

    type: str
    category: str
    drawn: bool = True

    @property
    def capabilities(self) -> List[str]:
        return list(_CAPABILITIES_BY_CATEGORY[self.category])


# The built-in widgets: name, category, and whether the dashboard draws them. A Toggle, Select, Slider or Text is a *setting* (`Section.setting`): it shows
# a value and lets the dashboard change it, so it is bidirectional. A Button (and the reserved Joystick) only sends.
_BUILT_IN_WIDGET_TYPES: Tuple[WidgetTypeInfo, ...] = (
    WidgetTypeInfo("Gauge", CATEGORY_OUTPUT),
    WidgetTypeInfo("Chart", CATEGORY_OUTPUT),
    WidgetTypeInfo("Status", CATEGORY_OUTPUT),
    WidgetTypeInfo("Video", CATEGORY_OUTPUT),
    WidgetTypeInfo("Button", CATEGORY_INPUT),
    WidgetTypeInfo("Toggle", CATEGORY_BIDIRECTIONAL),
    WidgetTypeInfo("Select", CATEGORY_BIDIRECTIONAL),
    WidgetTypeInfo("Slider", CATEGORY_BIDIRECTIONAL),
    WidgetTypeInfo("Text", CATEGORY_BIDIRECTIONAL),
    # Reserved: no dashboard component yet.
    WidgetTypeInfo("Joystick", CATEGORY_INPUT, drawn=False),
    # A component: video, state and named actions in one widget.
    WidgetTypeInfo("Camera", CATEGORY_BIDIRECTIONAL),
    # A component: folder listings and named actions (list, delete); the download itself is peer-to-peer, outside the command system.
    WidgetTypeInfo("Files", CATEGORY_BIDIRECTIONAL),
    # The dashboard adds a Heading to every program itself; a program never declares one.
    WidgetTypeInfo("Heading", CATEGORY_PRESENTATION),
    # Reserved: no dashboard component yet.
    WidgetTypeInfo("Folder", CATEGORY_PRESENTATION, drawn=False),
    WidgetTypeInfo("Tabs", CATEGORY_PRESENTATION, drawn=False),
    WidgetTypeInfo("Markdown", CATEGORY_PRESENTATION, drawn=False),
)

_REGISTRY: Dict[str, WidgetTypeInfo] = {info.type: info for info in _BUILT_IN_WIDGET_TYPES}


class WidgetRegistry:
    """Lookup of the built-in widget types."""

    @staticmethod
    def get(type: str) -> WidgetTypeInfo:
        try:
            return _REGISTRY[type]
        except KeyError:
            raise KeyError(f"unknown built-in widget type: {type!r}") from None

    @staticmethod
    def is_known(type: str) -> bool:
        return type in _REGISTRY

    @staticmethod
    def all() -> List[WidgetTypeInfo]:
        return list(_BUILT_IN_WIDGET_TYPES)


def capabilities_for(type: str) -> List[str]:
    """Default capabilities for a known built-in widget `type`."""
    return WidgetRegistry.get(type).capabilities
