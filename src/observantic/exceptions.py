"""
Exception hierarchy for Observantic.
All exceptions are designed to fail fast with clear error messages.
"""

from __future__ import annotations


class ObservanticException(Exception):
    """Base exception - fail fast on all errors."""
    pass


class WatcherException(ObservanticException):
    """File/database watching errors."""
    pass


class RecordCreationException(ObservanticException):
    """Record creation failures."""
    pass


class ConfigurationException(ObservanticException):
    """Invalid configuration or setup errors."""
    pass
