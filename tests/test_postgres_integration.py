"""Postgres integration tests (skip unless TEST_DATABASE_URL is set).

Uses observantic's public API end to end: make_store -> Postgres,
build_app -> bind -> watcher emit (auto_persist) -> where/get/history, plus
CAS conflicts and NotFound. Mirrors test_core_eventic on the SQLite backend.

The URL should use the postgresql+psycopg:// scheme: SQLAlchemy's plain
postgresql:// defaults to the psycopg2 driver, which eventic[postgres]
(psycopg 3) does not install. Bare postgresql:// URLs are translated by
make_store, but the explicit scheme is clearer. See devenv.nix enterTest for
the devenv Postgres example.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from eventic import Stream
from eventic.errors import NotFound, RevisionConflict
from pydantic import BaseModel

from observantic import EventWatcher, build_app, make_store

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason=(
        "TEST_DATABASE_URL not set (e.g. "
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic)"
    ),
)


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="pg_probe")


class EmittingWatcher(EventWatcher):
    stream: Stream | None = PROBE_STREAM


def test_make_store_postgres_and_outbox_capability():
    store = make_store(TEST_DATABASE_URL)
    try:
        assert "postgres" in str(store.engine.url)
        assert store.capabilities.outbox is True
    finally:
        store.close()


def test_watcher_emit_persists_through_bound_runtime():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-t", streams=[PROBE_STREAM]).bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)

        marker = f"/pg/{uuid4()}"  # unique per run: the DB persists across runs
        w._emit(path=marker, event_type="created", is_directory=False)
        w._emit(path=marker, event_type="modified", is_directory=False)

        page = runtime[PROBE_STREAM].where(path=marker)
        assert len(page.items) == 2  # one new aggregate per event
        assert all(it.revision == 0 for it in page.items)
    finally:
        store.close()


def test_cas_conflict_on_stale_base():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-cas", streams=[PROBE_STREAM]).bind(store)
        col = runtime[PROBE_STREAM]
        r0 = col.create(ProbeEvent(path="/cas"))
        r1 = col.change(r0, path="/cas2")
        assert r1.revision == 1
        assert col.get(r0.id, revision=0).state.path == "/cas"
        with pytest.raises(RevisionConflict):  # stale base (I7)
            col.change(r0, path="/cas3")
    finally:
        store.close()


def test_get_missing_raises_not_found():
    store = make_store(TEST_DATABASE_URL)
    try:
        runtime = build_app(id="pg-nf", streams=[PROBE_STREAM]).bind(store)
        with pytest.raises(NotFound):
            runtime[PROBE_STREAM].get(uuid4())
    finally:
        store.close()
