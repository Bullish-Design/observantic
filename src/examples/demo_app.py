#!/usr/bin/env python3
"""A ready-to-run eventic App over observantic's default streams.

Use it with the eventic CLI, e.g.:

    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify

``schema upgrade`` (Alembic) works with eventic v1.1.1+ (which ships
``alembic.ini``; v1.1.0's wheel could not) and is the way to bootstrap a
Postgres production schema:

    uv run eventic --app examples.demo_app:app --url "$DATABASE_URL" schema upgrade

SQLite creates its tables automatically on store construction.
"""

from __future__ import annotations

from eventic import App

from observantic import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM

app = App(
    id="observantic-demo",
    streams=[FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM],
)
