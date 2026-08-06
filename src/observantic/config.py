"""observantic.config
=====================
Global configuration for Observantic.

Settings are read from the environment **once, at import time**, and snapshot
into the immutable ``observantic.settings`` object.

Supported environment variables (documented in the README):

* ``OBSERVANTIC_DB_URL`` — database URL for eventic (sqlite:// or
  postgresql://); defaults to ``sqlite:///observantic.db`` (documented,
  preferred)
* ``DB_URL``             — backward-compatible alias; ``OBSERVANTIC_DB_URL``
                           wins when both are set
* ``OBSERVANTIC_LOG_LEVEL`` / ``LOG_LEVEL`` — Python logging level for the
  ``observantic`` logger

Why not a settings singleton?
------------------------------
Settings are read directly from the environment at import time and snapshot
into ``observantic.settings``. Earlier versions delegated to an external
singleton loader, which auto-created its singleton before any plugin mixin
could register — so the alias/mixin path could never observe the
environment (H-13/H-14). We therefore read the environment directly here.
``observantic.settings`` is the only settings object.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from pydantic import BaseModel, Field

__all__ = [
    "ObservanticSettings",
    "settings",
    "DB_URL",
    "LOG_LEVEL",
]


class ObservanticSettings(BaseModel):
    """Observantic configuration, snapshot at import time."""

    DB_URL: str = Field(
        default="sqlite:///observantic.db",
        description="Database URL for eventic (sqlite:// or postgresql://)",
    )
    LOG_LEVEL: str = Field(default="INFO", description="Python logging level")

    model_config = {"extra": "forbid"}


def _read_settings() -> dict[str, str]:
    """Read the documented env vars with backward-compatible aliases."""
    return {
        "DB_URL": (
            os.getenv("OBSERVANTIC_DB_URL")
            or os.getenv("DB_URL")
            or "sqlite:///observantic.db"
        ),
        "LOG_LEVEL": (
            os.getenv("OBSERVANTIC_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO"
        ),
    }


settings: Final[ObservanticSettings] = ObservanticSettings(**_read_settings())

# Convenience constants, consumed by the library (see observantic._eventic).
DB_URL: Final[str] = settings.DB_URL
LOG_LEVEL: Final[str] = settings.LOG_LEVEL

# Make LOG_LEVEL reach the library: configure our own logger at import time.
logging.getLogger("observantic").setLevel(LOG_LEVEL)
