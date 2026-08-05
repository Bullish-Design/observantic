"""Shared pytest fixtures that need no external services (no Postgres/DBOS)."""

from __future__ import annotations

import socket
import sqlite3

import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a small, ready-to-use SQLite database."""
    p = tmp_path / "test.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def free_port():
    """A port that was free at the moment of reservation."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _isolate_eventic():
    """Eventic is process-global (init once per process). Reset it before and
    after every test so ordering can never leak state between tests."""
    from observantic import reset

    reset()
    yield
    reset()
