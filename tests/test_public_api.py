"""Public API tests (Step 10): __all__, version consistency (H-18)."""

from __future__ import annotations

import importlib.metadata

import observantic


def test_version_consistent_with_metadata():
    assert observantic.__version__ == "0.4.0"
    assert importlib.metadata.version("observantic") == observantic.__version__


def test_all_exports_resolve():
    for name in observantic.__all__:
        assert hasattr(observantic, name), name


def test_eventic_integration_exports():
    assert callable(observantic.make_store)
    assert callable(observantic.build_app)


def test_default_streams_exports():
    assert observantic.FILE_STREAM.name == "files"
    assert observantic.SQLITE_STREAM.name == "sqlite"
    assert observantic.WEBHOOK_STREAM.name == "webhooks"


def test_settings_export():
    from observantic import settings

    assert hasattr(settings, "DB_URL")


def test_watchers_subclass_eventwatcher():
    from observantic import (
        EventWatcher,
        FileEventBase,
        SQLiteEventBase,
        WebhookEventBase,
    )

    assert issubclass(FileEventBase, EventWatcher)
    assert issubclass(SQLiteEventBase, EventWatcher)
    assert issubclass(WebhookEventBase, EventWatcher)


def test_exceptions_hierarchy():
    from observantic.exceptions import (
        ConfigurationException,
        ObservanticException,
        WatcherException,
    )

    assert issubclass(WatcherException, ObservanticException)
    assert issubclass(ConfigurationException, ObservanticException)


def test_examples_importable():
    """Every example module imports and its class definitions succeed (C-02)."""
    import examples.example_file  # noqa: F401
    import examples.example_webhook  # noqa: F401
    import examples.sqlite_example  # noqa: F401
    import examples.webhook_server  # noqa: F401
