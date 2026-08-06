#!/usr/bin/env python3
"""A ready-to-run eventic App over observantic's default streams.

Use it with the eventic CLI, e.g.:

    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db inspect
    uv run eventic --app examples.demo_app:app --url sqlite:///demo.db verify

``schema upgrade`` (Alembic) is for Postgres production only. eventic
v1.1.0's wheel omits its alembic.ini (untracked upstream), so the CLI
command fails until upstream ships a fix; prefer ``Postgres(url)`` with the
default ``create_tables=True`` for Postgres bootstrapping. SQLite creates
its tables automatically on store construction.
"""

from __future__ import annotations

from eventic import App

from observantic import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM

app = App(
    id="observantic-demo",
    streams=[FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM],
)
