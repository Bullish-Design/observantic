#!/usr/bin/env python3
"""A ready-to-run eventic App over observantic's default streams.

Use it with the eventic CLI, e.g.:

    uv run eventic --app examples.demo_app:app \
        --url sqlite:///demo.db schema upgrade
    uv run eventic --app examples.demo_app:app \
        --url sqlite:///demo.db inspect
"""

from __future__ import annotations

from eventic import App

from observantic import FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM

app = App(
    id="observantic-demo",
    streams=[FILE_STREAM, SQLITE_STREAM, WEBHOOK_STREAM],
)
