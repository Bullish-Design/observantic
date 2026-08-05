"""Watcher state machine tests: start/stop, validation rollback (H-10)."""

from __future__ import annotations

import pytest
from pydantic import PrivateAttr

from observantic import EventWatcher
from observantic.exceptions import ConfigurationException, WatcherException


class RecordingWatcher(EventWatcher):
    """A watcher with no-op extension points that records lifecycle calls."""

    _calls: list = PrivateAttr(default_factory=list)
    _impl_fail: bool = PrivateAttr(default=False)

    @property
    def calls(self):
        return self._calls

    def _validate_start(self, path: str) -> None:
        self.calls.append(("validate", path))

    def _start_impl(self, path: str, **kwargs):
        self.calls.append(("start", path, kwargs))
        if self._impl_fail:
            raise RuntimeError("impl boom")

    def _stop_impl(self):
        self.calls.append(("stop",))

    def on_start(self):
        self.calls.append(("on_start",))

    def on_stop(self):
        self.calls.append(("on_stop",))


def test_start_happy_path():
    w = RecordingWatcher()
    w.start_watching("/x", recursive=True)
    assert w._watching is True
    assert ("validate", "/x") in w.calls
    assert ("start", "/x", {"recursive": True}) in w.calls
    assert ("on_start",) in w.calls


def test_stop_when_not_watching_is_noop():
    w = RecordingWatcher()
    w.stop_watching()  # must not raise
    assert w._watching is False


def test_double_start_raises_watcher_exception():
    w = RecordingWatcher()
    w.start_watching("/x")
    with pytest.raises(WatcherException, match="Already watching"):
        w.start_watching("/y")
    w.stop_watching()


def test_start_rolls_back_on_validation_failure():
    """H-10: validation failure must leave _watching=False."""

    class W(EventWatcher):
        def _validate_start(self, path):
            raise ConfigurationException("nope")

    w = W()
    with pytest.raises(ConfigurationException):
        w.start_watching("/x")
    assert w._watching is False
    # and a second start must be allowed (state not poisoned)
    with pytest.raises(ConfigurationException):
        w.start_watching("/y")
    assert w._watching is False


def test_start_rolls_back_on_impl_failure():
    w = RecordingWatcher()
    w._impl_fail = True
    with pytest.raises(RuntimeError, match="impl boom"):
        w.start_watching("/x")
    assert w._watching is False
    assert ("on_start",) not in w.calls  # on_start not fired for a failed start


def test_stop_is_idempotent_and_fires_on_stop_once():
    w = RecordingWatcher()
    w.start_watching("/x")
    w.stop_watching()
    w.stop_watching()  # second stop: no-op
    assert w._watching is False
    assert w.calls.count(("on_stop",)) == 1


def test_stop_impl_failure_reported_and_on_stop_still_fires():
    calls = []

    class W(EventWatcher):
        def _validate_start(self, path):
            pass

        def _start_impl(self, path, **kwargs):
            pass

        def _stop_impl(self):
            raise RuntimeError("stop boom")

        def on_error(self, error, event=None):
            calls.append(("on_error", error))

        def on_stop(self):
            calls.append(("on_stop",))

    w = W()
    w.start_watching("/x")
    w.stop_watching()  # must not raise
    assert w._watching is False
    assert any(name == "on_error" for name, _ in calls)
    assert ("on_stop",) in calls


def test_restart_after_stop_works():
    w = RecordingWatcher()
    w.start_watching("/a")
    w.stop_watching()
    w.start_watching("/b")
    assert w._watching is True
    assert ("start", "/b", {}) in w.calls
    w.stop_watching()
