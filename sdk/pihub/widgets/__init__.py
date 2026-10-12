"""SDK-side widget model.

    from pihub.widgets import Output, Command, Presentation

`Output` and `Command` are runtime-endpoint widgets a `Program` registers
(returned by `Program.output()` / `Program.command()` - see program.py).
`Presentation` is the third widget category (layout/structure, capabilities
`["layout"]`); presentation belongs to the platform, so it carries no device
binding, no runtime behaviour, and is not registered through `Program`.

`WidgetRegistry` is the data half of a dashboard's widget registry: the catalog
of built-in widget types (type -> category/capabilities). The other half, the UI
component that draws each type, lives in the platform's frontend, not in this
SDK - see registry.py for details.
"""

from pihub.widgets.base import Widget
from pihub.widgets.command import Command, CommandHost
from pihub.widgets.output import Output, RuntimeHost
from pihub.widgets.presentation import Presentation
from pihub.widgets.setting import Setting
from pihub.widgets.registry import (
    CATEGORY_BIDIRECTIONAL,
    CATEGORY_INPUT,
    CATEGORY_OUTPUT,
    CATEGORY_PRESENTATION,
    WidgetRegistry,
    WidgetTypeInfo,
    capabilities_for,
)

__all__ = [
    "Setting",
    "Widget",
    "Output",
    "Command",
    "Presentation",
    "RuntimeHost",
    "CommandHost",
    "WidgetRegistry",
    "WidgetTypeInfo",
    "capabilities_for",
    "CATEGORY_OUTPUT",
    "CATEGORY_INPUT",
    "CATEGORY_BIDIRECTIONAL",
    "CATEGORY_PRESENTATION",
]
