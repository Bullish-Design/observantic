#!/usr/bin/env python
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pytest>=8.0.0",
#     "observantic",
#     "eventic>=0.1.5",
# ]
# ///
"""
Tests for Observantic library.
"""

from __future__ import annotations

import time
import sqlite3
from pathlib import Path
import pytest

from eventic import Eventic, Record
from observantic import FileEventBase, SQLiteEventBase, WatcherException


@pytest.fixture(scope="session", autouse=True)
def setup_eventic():
    """Initialize Eventic for all tests."""
    Eventic.init(
        name="observantic-test",
        database_url="postgresql://user:pass@localhost/test_db"
    )


@pytest.fixture
def temp_dir(tmp_path):
    """Create temporary directory for file tests."""
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
    
    def test_file_creation_triggers_record(self, temp_dir):
        """Test that file creation triggers Record creation."""
        events = []
        
        class TestWatcher(Record, FileEventBase):
            path: str
            
            def on_file_created(self, event):
                events.append(event.src_path)
                TestWatcher(path=event.src_path)
        
        watcher = TestWatcher()
        watcher.start_watching(str(temp_dir))
        
        # Create file
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")
        
        time.sleep(0.2)  # Allow event to process
        
        assert len(events) == 1
        assert str(test_file) in events[0]
        
        watcher.stop_watching()
    
    def test_pattern_filtering(self, temp_dir):
        """Test file pattern filtering."""
        pdf_events = []
        
        class PDFWatcher(Record, FileEventBase):
            watch_patterns = ["*.pdf"]
            
            def on_file_created(self, event):
                pdf_events.append(event.src_path)
        
        watcher = PDFWatcher()
        watcher.start_watching(str(temp_dir))
        
        # Create different files
        (temp_dir / "doc.pdf").write_text("pdf")
        (temp_dir / "text.txt").write_text("txt")
        
        time.sleep(0.2)
        
        assert len(pdf_events) == 1
        assert "doc.pdf" in pdf_events[0]
        
        watcher.stop_watching()
    
    def test_error_handling(self):
        """Test error handling for invalid paths."""
        class TestWatcher(Record, FileEventBase):
            pass
        
        watcher = TestWatcher()
        
        with pytest.raises(WatcherException, match="does not exist"):
            watcher.start_watching("/nonexistent/path")


class TestSQLiteEventBase:
    """Test SQLite monitoring."""
    
    def test_data_change_detection(self, temp_db):
        """Test detection of new rows in SQLite."""
        new_rows_captured = []
        
        class DBWatcher(Record, SQLiteEventBase):
            def on_data_changed(self, db_path, new_rows):
                new_rows_captured.extend(new_rows)
        
        watcher = DBWatcher()
        watcher.start_watching(temp_db)
        
        # Add data
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO test_table (data) VALUES (?)", ("test1",))
        cursor.execute("INSERT INTO test_table (data) VALUES (?)", ("test2",))
        conn.commit()
        conn.close()
        
        time.sleep(0.5)  # Allow detection
        
        assert len(new_rows_captured) == 2
        assert new_rows_captured[0].table_name == "test_table"
        assert new_rows_captured[0].row_data["data"] == "test1"
        
        watcher.stop_watching()
    
    def test_checkpoint_tracking(self, temp_db):
        """Test that checkpoints prevent duplicate processing."""
        total_rows = []
        
        class DBWatcher(Record, SQLiteEventBase):
            def on_data_changed(self, db_path, new_rows):
                total_rows.extend(new_rows)
        
        watcher = DBWatcher()
        watcher.start_watching(temp_db)
        
        # Add first batch
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO test_table (data) VALUES (?)", ("row1",))
        conn.commit()
        
        time.sleep(0.5)
        
        # Add second batch
        cursor.execute("INSERT INTO test_table (data) VALUES (?)", ("row2",))
        conn.commit()
        conn.close()
        
        time.sleep(0.5)
        
        # Should have exactly 2 rows, not duplicates
        assert len(total_rows) == 2
        assert total_rows[0].row_data["data"] == "row1"
        assert total_rows[1].row_data["data"] == "row2"
        
        watcher.stop_watching()


def test_multiple_inheritance():
    """Test that multiple inheritance works correctly."""
    
    class CombinedWatcher(Record, FileEventBase, SQLiteEventBase):
        path: str
        event_count: int = 0
        
        def on_file_created(self, event):
            self.event_count += 1
        
        def on_data_changed(self, db_path, new_rows):
            self.event_count += len(new_rows)
    
    watcher = CombinedWatcher(path="test")
    assert hasattr(watcher, "start_watching")
    assert hasattr(watcher, "on_file_created")
    assert hasattr(watcher, "on_data_changed")
    assert watcher.event_count == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
