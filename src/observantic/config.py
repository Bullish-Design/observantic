from __future__ import annotations

"""observantic.config
=====================
Global configuration layer built on **Confidantic**.  A single
:class:`ObservanticSettings` instance is created lazily and shared across the
process, giving every watcher access to database URLs, log levels and version
metadata *without* manual plumbing.

Design goals
------------
* **Zero‑boilerplate** – defaults "just work" in local dev.
* **Predictable overrides** – all vars can be changed via ENV or `.env`.
* **Git‑aware** – semantic version and commit SHA are auto‑injected when the
  current working directory is a Git repo (courtesy of Confidantic's
  ``GitInfo`` helper).
* **Type‑safe** – powered by Pydantic v2, so every attribute is validated at
  import‑time.

Usage::

    from observantic import settings
    print(settings.DB_URL)

``settings`` is an *already‑instantiated* singleton – but creating a bespoke
instance is as simple as ``ObservanticSettings(_env_file=".env.test")``.
"""

from pathlib import Path
from typing import Final, Optional

from confidantic import Settings as _BaseSettings, GitInfo
from pydantic import Field, field_validator

__all__ = [
    "ObservanticSettings",
    "settings",
]


class ObservanticSettings(_BaseSettings):
    """Project‑wide configuration for Observantic.

    All attributes can be overridden via environment variables prefixed with
    ``OBSERVANTIC_`` (unless a custom prefix is provided).
    """

    # ------------------------------------------------------------------
    # Core runtime options
    # ------------------------------------------------------------------
    DB_URL: str = Field(
        default="dbos://default",
        description="Database/queue URL consumed by Eventic.",
    )
    LOG_LEVEL: str = Field(
        default="INFO", description="Root log level used by `logging`.")

    # ------------------------------------------------------------------
    # Metadata – dynamically resolved (Git, semantic version, etc.)
    # ------------------------------------------------------------------
    VERSION: Optional[str] = Field(
        default=None,
        description="Semantic version derived from Git tags, if any.",
    )
    COMMIT_SHA: Optional[str] = Field(
        default=None, description="Current commit SHA if in a Git repo.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    _ENV_PREFIX: Final[str] = "OBSERVANTIC_"

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_level(cls, v: str) -> str:  # noqa: D401, N802
        upper = v.upper()
        if upper not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be a valid stdlib logging level")
        return upper

    def _post_init_post_parse__(self) -> None:  # noqa: D401
        """Populate Git metadata lazily after core validation."""

        if self.VERSION is None or self.COMMIT_SHA is None:
            try:
                git = GitInfo(Path.cwd())
                self.VERSION = self.VERSION or git.version
                self.COMMIT_SHA = self.COMMIT_SHA or git.commit
            except Exception:  # pragma: no cover – Git optional
                # Running outside a Git repo; leave as‑is.
                pass

    class Config:
        env_prefix = "OBSERVANTIC_"
        case_sensitive = False
        extra = "ignore"


# ---------------------------------------------------------------------------
# Singleton – import‑time settings instance usable across the entire package.
# ---------------------------------------------------------------------------
settings: Final[ObservanticSettings] = ObservanticSettings()
