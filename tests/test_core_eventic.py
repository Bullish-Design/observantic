"""Tests for the Eventic seam (observantic._eventic): unwrap, readiness, emit,
and real SQLite-backed persistence (no Postgres required — Eventic 0.1.5's
DBOS 2.x defaults the system DB to the app DB for sqlite URLs)."""

from __future__ import annotations

import inspect
import logging

import pytest
from eventic import Record
from eventic.queues.dispatcher import evented
from pydantic import BaseModel, PrivateAttr

from observantic import EventWatcher, is_eventic_ready, reset
from observantic._eventic import call_unwrapped
from observantic.exceptions import ConfigurationException

# ---------------------------------------------------------------------------
# Module-level Record subclasses. Eventic 0.1.5 declares a DBOS Queue per
# Record class *at class-definition time*, keyed by class name, and raises on
# duplicates — so names must be unique process-wide and never redefined.
# ---------------------------------------------------------------------------


class _UnwrapProbe(Record):
    """Undecorated + @evented hooks for call_unwrapped tests."""

    value: int = 0

    def plain_hook(self, event):
        return "plain-raw"

    @evented
    def queued_hook(self, event):
        return "queued-raw"


class _DispatchProbe(Record, EventWatcher):
    """Record-based watcher: dispatch must work without init/launch."""

    _calls: list = PrivateAttr(default_factory=list)

    def on_file_created(self, event):
        self._calls.append("hook ran")


class _PersistedProbe(Record):
    value: int = 0


class _PersistedWatcher(EventWatcher):
    record_model: type[BaseModel] = _PersistedProbe


class SimpleRecord(BaseModel):
    x: int


class EmittingWatcher(EventWatcher):
    def _default_record_model(self):
        return SimpleRecord


# ---------------------------------------------------------------------------
# call_unwrapped — Eventic 0.1.5 wraps only @evented methods (C-03)
# ---------------------------------------------------------------------------


def test_call_unwrapped_returns_none_for_missing_method():
    assert call_unwrapped(EventWatcher, "on_file_created") is None


def test_call_unwrapped_passthrough_for_plain_method():
    class W(EventWatcher):
        def on_file_created(self, event):
            return "raw"

    raw = call_unwrapped(W, "on_file_created")
    assert raw is not None
    assert raw(W(), object()) == "raw"


def test_undecorated_record_method_is_not_wrapped():
    """RecordMeta no longer wraps undecorated methods — inspect.unwrap is a
    no-op passthrough (the guide's 'either way' contract)."""
    fn = _UnwrapProbe.plain_hook
    assert not hasattr(fn, "__wrapped__")  # left completely untouched
    raw = call_unwrapped(_UnwrapProbe, "plain_hook")
    assert raw is fn
    assert raw(_UnwrapProbe(), object()) == "plain-raw"


def test_evented_record_method_is_unwrapped_to_raw():
    """@evented methods are wrapped (functools.wraps chain) but call_unwrapped
    recovers the raw function so dispatch never touches DBOS queues."""
    wrapped = _UnwrapProbe.queued_hook
    assert hasattr(wrapped, "__wrapped__")
    raw = call_unwrapped(_UnwrapProbe, "queued_hook")
    assert raw is not None
    assert raw(_UnwrapProbe(), object()) == "queued-raw"


def test_record_dispatch_without_init_does_not_crash():
    """C-03 repro: a Record-based watcher dispatches with NO DBOSException and
    no double execution, even before any Eventic init/launch."""
    w = _DispatchProbe()
    evt = object()
    w._dispatch_hook("on_file_created", evt)  # returns normally
    assert w._calls == ["hook ran"]  # exactly once


# ---------------------------------------------------------------------------
# is_eventic_ready / init / reset
# ---------------------------------------------------------------------------


def test_is_eventic_ready_false_before_init():
    assert is_eventic_ready() is False
    from observantic._eventic import is_launched

    assert is_launched() is False  # not launched before init


def test_reset_after_never_initializing_is_noop():
    reset()  # must not raise when Eventic was never initialized


def test_init_defaults_to_settings_db_url():
    """init() with no database_url lets the seam fall back to settings.DB_URL (H-14)."""
    from observantic import _eventic

    sig = inspect.signature(_eventic.init_eventic)
    assert (
        sig.parameters["database_url"].default == "postgresql://localhost/observantic"
    )


def test_init_forwards_name(monkeypatch):
    from observantic import init

    seen = {}

    def fake_init_eventic(*, name, database_url=None, **kwargs):
        seen["name"] = name
        seen["database_url"] = database_url
        return "eventic"

    import observantic

    monkeypatch.setattr(observantic, "init_eventic", fake_init_eventic)
    assert init("probe-app") == "eventic"
    assert seen == {"name": "probe-app", "database_url": None}


def test_init_passes_explicit_database_url(monkeypatch):
    from observantic import init

    seen = {}

    def fake_init_eventic(*, name, database_url, **kwargs):
        seen["database_url"] = database_url
        return None

    import observantic

    monkeypatch.setattr(observantic, "init_eventic", fake_init_eventic)
    init("probe-app", "postgresql://explicit:5432/db")
    assert seen["database_url"] == "postgresql://explicit:5432/db"


# ---------------------------------------------------------------------------
# _emit / auto_persist (C-08)
# ---------------------------------------------------------------------------


def test_emit_creates_default_record_without_touching_eventic():
    w = EmittingWatcher()
    rec = w._emit(x=42)
    assert isinstance(rec, SimpleRecord)
    assert rec.x == 42


def test_emit_uses_record_model_when_set():
    w = EmittingWatcher(record_model=_PersistedProbe)
    rec = w._emit(value=7)
    assert isinstance(rec, _PersistedProbe)
    assert rec.value == 7


def test_auto_persist_without_init_warns_and_returns_record(caplog):
    """auto_persist=True + no init() → warning logged, record still returned."""
    w = EmittingWatcher(auto_persist=True)
    with caplog.at_level(logging.WARNING, logger="observantic"):
        rec = w._emit(x=1)
    assert rec is not None
    assert "not persisted" in caplog.text


def test_auto_persist_strict_without_init_raises():
    w = EmittingWatcher(auto_persist=True, persist_strict=True)
    with pytest.raises(ConfigurationException):
        w._emit(x=1)


def test_auto_persist_default_is_false():
    assert EmittingWatcher().auto_persist is False


# ---------------------------------------------------------------------------
# Real persistence — SQLite backend, no Postgres needed (Eventic 0.1.5)
# ---------------------------------------------------------------------------


def test_persist_with_sqlite_backend(tmp_path):
    """With a wired store, _emit constructs the record, Eventic persists the
    durable v0 row at construction, and auto_persist's append is idempotent."""
    from observantic import init

    db_url = f"sqlite:///{tmp_path / 'eventic.db'}"
    init(name="obs-sqlite-persist", database_url=db_url)
    try:
        assert is_eventic_ready()

        w = _PersistedWatcher(auto_persist=True)
        rec = w._emit(value=7)
        assert isinstance(rec, _PersistedProbe)
        assert rec.value == 7

        # v0 row persisted at construction; re-append is an idempotent no-op.
        fresh = _PersistedProbe.hydrate(rec.id)
        assert fresh.id == rec.id
        assert fresh.value == 7
        assert fresh.version == 0

        # auto_persist=False still constructs safely with a wired store.
        w2 = _PersistedWatcher()
        rec2 = w2._emit(value=8)
        assert rec2.value == 8
    finally:
        reset()
        assert is_eventic_ready() is False


def test_reset_allows_reinit(tmp_path):
    """Eventic 0.1.5 is once-per-process; reset() must allow a fresh init."""
    from observantic import init

    init(name="obs-reset-a", database_url=f"sqlite:///{tmp_path / 'a.db'}")
    reset()
    assert is_eventic_ready() is False
    init(name="obs-reset-b", database_url=f"sqlite:///{tmp_path / 'b.db'}")
    assert is_eventic_ready() is True
