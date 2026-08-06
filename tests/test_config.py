"""Config tests (Step 2/10): env aliases, defaults, precedence (H-13/H-14)."""

from __future__ import annotations

from observantic.config import ObservanticSettings, _read_settings


def test_defaults():
    s = ObservanticSettings()
    assert s.DB_URL == "sqlite:///observantic.db"
    assert s.LOG_LEVEL == "INFO"


def test_read_settings_prefers_observantic_prefix(monkeypatch):
    monkeypatch.setenv("OBSERVANTIC_DB_URL", "postgresql://prefixed/db")
    monkeypatch.setenv("DB_URL", "postgresql://plain/db")
    monkeypatch.setenv("OBSERVANTIC_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    values = _read_settings()
    assert values["DB_URL"] == "postgresql://prefixed/db"
    assert values["LOG_LEVEL"] == "DEBUG"


def test_read_settings_falls_back_to_plain_alias(monkeypatch):
    monkeypatch.delenv("OBSERVANTIC_DB_URL", raising=False)
    monkeypatch.setenv("DB_URL", "postgresql://plain/db")
    values = _read_settings()
    assert values["DB_URL"] == "postgresql://plain/db"


def test_read_settings_defaults(monkeypatch):
    monkeypatch.delenv("OBSERVANTIC_DB_URL", raising=False)
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("OBSERVANTIC_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    values = _read_settings()
    assert values == {
        "DB_URL": "sqlite:///observantic.db",
        "LOG_LEVEL": "INFO",
    }


def test_extra_fields_rejected():
    import pytest

    with pytest.raises(ValueError):
        ObservanticSettings.model_validate({"NOPE": 1})
