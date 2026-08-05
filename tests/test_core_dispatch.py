"""Core dispatch tests: hooks, overrides, error containment (C-03/C-04/H-12)."""

from __future__ import annotations

import pytest

from observantic import EventWatcher
from observantic.exceptions import ConfigurationException


def _call_recorder():
    return []


def test_registered_callback_fires():
    calls = _call_recorder()

    def cb(event):
        calls.append(event)

    w = EventWatcher()
    w.register_hook("on_file_created", cb)
    evt = object()
    w._dispatch_hook("on_file_created", evt)
    assert calls == [evt]


def test_override_fires():
    calls = _call_recorder()

    class W(EventWatcher):
        def on_file_created(self, event):
            calls.append(event)

    w = W()
    evt = object()
    w._dispatch_hook("on_file_created", evt)
    assert calls == [evt]


def test_override_and_registered_both_fire():
    calls = _call_recorder()

    class W(EventWatcher):
        def on_file_created(self, event):
            calls.append(("override", event))

    def cb(event):
        calls.append(("callback", event))

    w = W()
    w.register_hook("on_file_created", cb)
    evt = object()
    w._dispatch_hook("on_file_created", evt)
    assert calls == [("override", evt), ("callback", evt)]


def test_unregister_removes_callback():
    calls = _call_recorder()

    def cb(event):
        calls.append(event)

    w = EventWatcher()
    w.register_hook("on_file_created", cb)
    w.unregister_hook("on_file_created", cb)
    w._dispatch_hook("on_file_created", object())
    assert calls == []


def test_invalid_hook_registration_raises():
    w = EventWatcher()
    with pytest.raises(ConfigurationException, match="callable"):
        w.register_hook("on_file_created", "not-callable")  # type: ignore[arg-type]


def test_hook_error_does_not_raise():
    """A raising hook must NOT propagate out of _dispatch_hook (C-04)."""

    class W(EventWatcher):
        def on_file_created(self, event):
            raise ValueError("boom")

    w = W()
    w._dispatch_hook("on_file_created", object())  # returns normally


def test_hook_error_reports_event_object_to_on_error():
    """on_error receives the event object, not the hook name (H-12)."""
    seen = {}

    class W(EventWatcher):
        def on_file_created(self, event):
            raise ValueError("boom")

        def on_error(self, error, event=None):
            seen["error"] = error
            seen["event"] = event

    w = W()
    evt = object()
    w._dispatch_hook("on_file_created", evt)
    assert isinstance(seen["error"], ValueError)
    assert seen["event"] is evt


def test_registered_callback_error_also_reported():
    seen = {}

    def bad(event):
        raise KeyError("nope")

    class W(EventWatcher):
        def on_error(self, error, event=None):
            seen["error"] = error
            seen["event"] = event

    w = W()
    w.register_hook("on_file_created", bad)
    evt = object()
    w._dispatch_hook("on_file_created", evt)
    assert isinstance(seen["error"], KeyError)
    assert seen["event"] is evt


def test_raise_on_hook_error_collects():
    class W(EventWatcher):
        def on_file_created(self, event):
            raise ValueError("boom")

    w = W(raise_on_hook_error=True)
    w._dispatch_hook("on_file_created", object())
    assert isinstance(w._last_hook_error, ValueError)


def test_last_hook_error_cleared_between_dispatches():
    class W(EventWatcher):
        def on_file_created(self, event):
            raise ValueError("boom")

    w = W(raise_on_hook_error=True)
    w._dispatch_hook("on_file_created", object())
    assert isinstance(w._last_hook_error, ValueError)
    w._dispatch_hook("on_file_modified", object())  # no override, no error
    assert w._last_hook_error is None


def test_no_dispatch_without_override_or_callback_is_noop():
    w = EventWatcher()
    w._dispatch_hook("on_file_created", object())  # no-op, no error


def test_failing_lifecycle_hook_is_logged_not_raised(caplog):
    """A failing on_start must be logged, never raised (observer survives)."""

    class W(EventWatcher):
        def on_start(self):
            raise RuntimeError("bad on_start")

    import logging

    w = W()
    with caplog.at_level(logging.ERROR, logger="observantic"):
        w._safe_call("on_start")
    assert "bad on_start" in caplog.text
    assert w._watching is False  # never flipped
