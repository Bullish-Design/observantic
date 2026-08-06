"""Shared pytest fixtures (no external services; SQLite is the test backend)."""

from __future__ import annotations

import socket
import sqlite3

import pytest
from eventic.sql import SQLite


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


@pytest.fixture
def store():
    """An in-memory eventic store; tables are created on construction."""
    s = SQLite(":memory:")
    yield s
    s.close()
