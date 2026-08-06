"""Optional Postgres integration tests (skipped without TEST_DATABASE_URL).

Run against the devenv Postgres, e.g.:

    TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@127.0.0.1:5432/eventic \
      uv run pytest tests/test_postgres_integration.py
"""

from __future__ import annotations

import os

import pytest
from eventic import App, Stream
from pydantic import BaseModel

from observantic import make_store

URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not URL, reason="TEST_DATABASE_URL not set (needs a live Postgres)"
)


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="pg_probe")


def test_make_store_roundtrip_on_postgres():
    store = make_store(URL)
    try:
        # bare postgresql:// URLs are translated to the psycopg3 driver
        assert store.engine.url.drivername == "postgresql+psycopg"
        runtime = App(id="pg-test", streams=[PROBE_STREAM]).bind(store)
        runtime[PROBE_STREAM].create(ProbeEvent(path="/pg", event_type="created"))
        items = runtime[PROBE_STREAM].where(path="/pg").items
        assert len(items) == 1
        assert items[0].state.path == "/pg"
    finally:
        store.close()
