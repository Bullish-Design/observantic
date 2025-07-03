#!/usr/bin/env python3
# /// script
# dependencies = [
#     "pytest>=8.0.0",
#     "observantic>=0.2.0",
#     "eventic>=0.1.5",
# ]
# ///
"""
Tests for Observantic watchers.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest
from eventic import Record
from observantic import FileEventBase, SQLiteEventBase, WebhookEventBase, init


@pytest.fixture(scope="session", autouse=True)
def setup_eventic():
    """Initialize Eventic for tests."""
    init(
        name="observantic-test",
        database_url="postgresql://user:pass@localhost/test_db"
    )


@pytest.fixture
def temp_dir(tmp_path):
    """Create temporary directory for tests."""
    test_dir = tmp_path / "test_files"
    test_dir.mkdir()
    yield test_dir


@pytest.fixture
def temp_db(tmp_path):
    """Create temporary SQLite database."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE test_table (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT
        )
    ''')
    conn.commit()
    conn.close()
    yield str(db_path)


class TestFileEventBase:
    """Test file system monitoring."""
    
    def test_start_stop(self, temp_dir):
        """Test watcher starts and stops correctly."""
        
        class TestWatcher(Record, FileEventBase):
            path: str
        
        watcher = TestWatcher()
        watcher.start_watching(str(temp_dir))
        
        assert watcher._watching is True
        assert watcher._observer is not None
        assert watcher._observer.is_alive()
        
        watcher.stop_watching()
        
        assert watcher._watching is False
        assert not watcher._observer.is_alive()
    
    def test_file_events(self, temp_dir):
        """Test file event detection."""
        events = []
        
        class TestWatcher(Record, FileEventBase):
            path: str
            
            def on_file_created(self, event):
                events.append(("created", event.src_path))
        
        watcher = TestWatcher()
        watcher.start_watching(str(temp_dir))
        
        try:
            # Create file
            test_file = temp_dir / "test.txt"
            test_file.write_text("content")
            
            time.sleep(0.2)  # Allow event processing
            
            assert len(events) == 1
            assert events[0][0] == "created"
            assert "test.txt" in events[0][1]
        finally:
            watcher.stop_watching()
    
    def test_double_start_error(self, temp_dir):
        """Test that starting twice raises error."""
        
        class TestWatcher(Record, FileEventBase):
            path: str
        
        watcher = TestWatcher()
        watcher.start_watching(str(temp_dir))
        
        try:
            with pytest.raises(RuntimeError, match="Already watching"):
                watcher.start_watching(str(temp_dir))
        finally:
            watcher.stop_watching()


class TestWebhookEventBase:
    """Test webhook server."""
    
    def test_server_lifecycle(self):
        """Test server starts and stops."""
        
        class TestWebhook(Record, WebhookEventBase):
            endpoint: str
            port = 18888  # Non-standard port
        
        server = TestWebhook()
        server.start_watching()
        
        assert server._server is not None
        assert server._server_thread.is_alive()
        
        server.stop_watching()
        
        assert not server._server_thread.is_alive()
    
    def test_lifecycle_hooks(self):
        """Test on_start and on_stop hooks."""
        calls = []
        
        class HookedWebhook(Record, WebhookEventBase):
            endpoint: str
            port = 18889
            
            def on_start(self):
                calls.append("started")
            
            def on_stop(self):
                calls.append("stopped")
        
        server = HookedWebhook()
        server.start_watching()
        server.stop_watching()
        
        assert calls == ["started", "stopped"]


def test_hook_registration():
    """Test dynamic hook registration."""
    
    called = []
    
    def custom_hook(event):
        called.append(event)
    
    class HookTest(Record, FileEventBase):
        path: str
    
    watcher = HookTest()
    watcher.register_hook("on_file_created", custom_hook)
    
    # Simulate dispatch
    watcher._dispatch_hook("on_file_created", "test-event")
    
    assert len(called) == 1
    assert called[0] == "test-event"


def test_invalid_hook_registration():
    """Test invalid hook registration."""
    
    class TestWatcher(Record, FileEventBase):
        path: str
    
    watcher = TestWatcher()
    
    with pytest.raises(ValueError, match="Hook must be callable"):
        watcher.register_hook("test", "not-callable")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
