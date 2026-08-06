"""Tests for the Eventic seam (observantic._eventic) and persistence.

Eventic 1.1.0 is declaration-based: streams are frozen values, writes go
through a bound Collection, and there is no process-global state.
"""

from __future__ import annotations

import logging

import pytest
from eventic import App, Stream
from eventic.errors import NotFound, RevisionConflict
from eventic.sql import SQLite
from pydantic import BaseModel, ValidationError

from observantic import EventWatcher, build_app, make_store
from observantic.exceptions import ConfigurationException
from observantic.monitors import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


class StrictProbe(BaseModel):
    """A strict model: unknown emit kwargs raise loudly, like the default
    monitor record models (which use ``extra="forbid"``)."""

    path: str = ""
    event_type: str = ""

    model_config = {"extra": "forbid"}


PROBE_STREAM = Stream(ProbeEvent, name="probe")
STRICT_STREAM = Stream(StrictProbe, name="strict")


class EmittingWatcher(EventWatcher):
    """A watcher with a stream; persistence is opt-in."""

    stream: Stream | None = PROBE_STREAM


# ---------------------------------------------------------------------------
# make_store / build_app / default streams
# ---------------------------------------------------------------------------


def test_make_store_sqlite(tmp_path):
    store = make_store(f"sqlite:///{tmp_path / 'x.db'}")
    try:
        assert store.capabilities.outbox is True  # outbox works on sqlite
    finally:
        store.close()


def test_make_store_bare_path(tmp_path):
    store = make_store(str(tmp_path / "bare.db"))
    try:
        store.close()  # tables were created on construction
    finally:
        store.close()


def test_make_store_rejects_bad_scheme():
    with pytest.raises(ConfigurationException, match="scheme"):
        make_store("oracle://host/db")


def test_make_store_defaults_to_settings(monkeypatch, tmp_path):
    import observantic._eventic as seam

    monkeypatch.setattr(seam, "DEFAULT_DB_URL", f"sqlite:///{tmp_path / 'default.db'}")
    store = make_store()
    try:
        assert "sqlite" in str(store.engine.url)
    finally:
        store.close()


def test_build_app_collects_streams():
    app = build_app(id="t", streams=[PROBE_STREAM])
    assert [s.name for s in app.streams] == ["probe"]


def test_default_streams_have_stable_names():
    assert FILE_STREAM.name == "files"
    assert SQLITE_STREAM.name == "sqlite"
    assert WEBHOOK_STREAM.name == "webhooks"
    assert FILE_STREAM.model.__name__ == "FileRecord"
    assert SQLITE_STREAM.model.__name__ == "DatabaseRow"
    assert WEBHOOK_STREAM.model.__name__ == "WebhookRecord"


def test_stream_name_validation():
    # eventic validates stream names via a pydantic BeforeValidator, which
    # raises ValueError (not ConfigError) for invalid names.
    with pytest.raises(ValueError):
        Stream(ProbeEvent, name="Not Valid!")


# ---------------------------------------------------------------------------
# _emit / auto_persist / bind
# ---------------------------------------------------------------------------


def test_emit_creates_state_without_touching_eventic():
    w = EmittingWatcher()
    state = w._emit(path="/x", event_type="created", is_directory=False)
    assert isinstance(state, ProbeEvent)
    assert state.path == "/x"


def test_emit_uses_custom_stream_model():
    class Custom(BaseModel):
        path: str = ""
        event_type: str = ""
        is_directory: bool = False

    custom = Stream(Custom, name="custom")
    w = EmittingWatcher(stream=custom)
    state = w._emit(path="/y", event_type="modified", is_directory=False)
    assert isinstance(state, Custom)


def test_emit_unknown_kwarg_fails_loudly():
    w = EmittingWatcher(stream=STRICT_STREAM)
    with pytest.raises(ValidationError):  # pydantic ValidationError, not silent drop
        w._emit(bogus=1)


def test_auto_persist_default_is_false():
    assert EmittingWatcher().auto_persist is False


def test_auto_persist_without_bind_warns_and_returns_state(caplog):
    w = EmittingWatcher(auto_persist=True)
    with caplog.at_level(logging.WARNING, logger="observantic"):
        state = w._emit(path="/x", event_type="created", is_directory=False)
    assert state is not None
    assert "not bound" in caplog.text


def test_auto_persist_strict_without_bind_raises():
    w = EmittingWatcher(auto_persist=True, persist_strict=True)
    with pytest.raises(ConfigurationException, match="not bound"):
        w._emit(path="/x", event_type="created", is_directory=False)


def test_bind_requires_stream():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EventWatcher()  # no stream
        with pytest.raises(ConfigurationException, match="no stream"):
            w.bind(runtime)
    finally:
        store.close()


def test_bind_rejects_uninstalled_stream():
    store = SQLite(":memory:")
    try:
        other = Stream(ProbeEvent, name="other")
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(stream=other)
        with pytest.raises(ConfigurationException, match="not installed"):
            w.bind(runtime)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Real persistence — SQLite backend
# ---------------------------------------------------------------------------


def test_auto_persist_commits_create_to_store():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)

        w._emit(path="/a", event_type="created", is_directory=False)

        page = runtime[PROBE_STREAM].where(path="/a")
        assert len(page.items) == 1
        assert page.items[0].state.path == "/a"
        assert page.items[0].revision == 0  # one new aggregate per event
    finally:
        store.close()


def test_emit_without_autopersist_does_not_persist():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher()
        w.bind(runtime)
        w._emit(path="/a", event_type="created", is_directory=False)
        assert runtime[PROBE_STREAM].where(path="/a").items == ()
    finally:
        store.close()


def test_change_writes_revision_and_cas_conflicts():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        col = runtime[PROBE_STREAM]
        r0 = col.create(ProbeEvent(path="/a"))
        r1 = col.change(r0, path="/b")
        assert r1.revision == 1
        assert col.get(r0.id, revision=0).state.path == "/a"
        assert col.get(r0.id).state.path == "/b"
        assert [r.revision for r in col.history(r0.id).items] == [0, 1]

        # stale base -> loud conflict (I7)
        with pytest.raises(RevisionConflict):
            col.change(r0, path="/c")
    finally:
        store.close()


def test_get_missing_raises_not_found():
    from uuid import uuid4

    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        with pytest.raises(NotFound):
            runtime[PROBE_STREAM].get(uuid4())
    finally:
        store.close()


def test_unbind_stops_persistence():
    store = SQLite(":memory:")
    try:
        runtime = App(id="t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True, persist_strict=True)
        w.bind(runtime)
        w._emit(path="/a", event_type="created", is_directory=False)
        w.unbind()
        with pytest.raises(ConfigurationException, match="not bound"):
            w._emit(path="/b", event_type="created", is_directory=False)
    finally:
        store.close()
