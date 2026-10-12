"""Presentation widget (capabilities `["layout"]`): never communicates with devices, has
no runtime state and no device binding.

Presentation widgets (Heading, Folder, Tabs, Markdown, ...) describe pure
layout/structure. They belong to the platform, not the device: unlike
Output/Command there is no `program.presentation()` - `Program`'s API only
defines `output()` and `command()`. `Presentation` is included here as the third member of
the widget taxonomy, matching the SDK's own package layout
(`widgets/presentation.py`), as a plain data class with no host binding
and no runtime behaviour - `set()`, `set_values()` and `handler()` simply
don't exist on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, List

from pihub.protocol.messages import CAPABILITY_LAYOUT
from pihub.widgets.base import Widget


@dataclass
class Presentation(Widget):
    """A layout/structure widget with no device binding or runtime state."""

    CAPABILITIES: ClassVar[List[str]] = [CAPABILITY_LAYOUT]
