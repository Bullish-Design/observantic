from __future__ import annotations

"""observantic.config
=====================
Global configuration for Observantic using Confidantic.
"""

from pathlib import Path
from typing import Final, Optional

from confidantic import SettingsType, PluginRegistry
from pydantic import Field, field_validator

__all__ = [
    "ObservanticSettings",
    "settings",
]


class ObservanticMixin(SettingsType):
    """Observantic-specific settings mixed into Confidantic."""

    # Database/queue URL for Eventic
    DB_URL: str = Field(
        default="postgresql://localhost/observantic",
        description="Database URL for Eventic",
    )

    # Logging
    LOG_LEVEL: str = Field(default="INFO", description="Python logging level")


# Register our mixin with Confidantic
PluginRegistry.register(ObservanticMixin)

# Build the final settings class
ObservanticSettings = PluginRegistry.build_class()

# Create singleton instance
settings: Final[ObservanticSettings] = ObservanticSettings()
