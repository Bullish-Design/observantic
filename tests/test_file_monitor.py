"""File monitor integration tests (Step 6): events, throttle, survival."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from observantic import FileEventBase
from observantic.exceptions import ConfigurationException


def wait_for(predicate, timeout=5.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_create_modify_delete_move_fire_hooks(tmp_path):
    events = []

    class W(FileEventBase):
        def on_file_created(self, event):
            events.append(("created", Path(event.src_path).name, event.event_type))

        def on_file_modified(self, event):
            events.append(("modified", Path(event.src_path).name, event.event_type))

        def on_file_deleted(self, event):
            events.append(("deleted", Path(event.src_path).name, event.event_type))

        def on_file_moved(self, event):
            events.append(("moved", Path(event.src_path).name, event.event_type))

    w = W(watch_patterns=["*.pdf"])
    w.start_watching(str(tmp_path))

    try:
        f = tmp_path / "doc.pdf"
        f.write_text("v1")
        time.sleep(0.15)
        f.write_text("v2")
        time.sleep(0.15)
        f2 = tmp_path / "doc2.pdf"
        f.rename(f2)
        time.sleep(0.15)
        f2.unlink()
        time.sleep(0.15)

        assert wait_for(lambda: any(e[0] == "created" for e in events)), events
        assert wait_for(lambda: any(e[0] == "modified" for e in events)), events
        assert wait_for(lambda: any(e[0] == "deleted" for e in events)), events
        assert wait_for(lambda: any(e[0] == "moved" for e in events)), events
        kinds = {e[0] for e in events}
        assert "created" in kinds and "modified" in kinds
        assert "deleted" in kinds and "moved" in kinds
        # watchdog events carry event_type
        assert all(e[2] == e[0] for e in events)
    finally:
        w.stop_watching()


def test_patterns_filter_non_matching_files(tmp_path):
    events = []

    class W(FileEventBase):
        def on_file_created(self, event):
            events.append(event)

    w = W(watch_patterns=["*.txt"])
    w.start_watching(str(tmp_path))
    try:
        (tmp_path / "data.txt").write_text("x")
        (tmp_path / "data.pdf").write_text("x")
        assert wait_for(lambda: len(events) >= 1)
        time.sleep(0.3)
        assert len(events) == 1  # only the .txt fired
        assert events[0].src_path.endswith("data.txt")
    finally:
        w.stop_watching()


def test_directory_events_ignored(tmp_path):
    events = []

    class W(FileEventBase):
        def on_file_created(self, event):
            events.append(event)

    w = W()
    w.start_watching(str(tmp_path))
    try:
        (tmp_path / "subdir").mkdir()
        time.sleep(0.4)
        assert events == []  # directory creation is ignored
    finally:
        w.stop_watching()


def test_throttle_coalesces_bursts(tmp_path):
    events = []

    class W(FileEventBase):
        def on_file_created(self, event):
            events.append(event.src_path)

        def on_file_modified(self, event):
            events.append(event.src_path)

    w = W(event_throttle_seconds=1.0)
    w.start_watching(str(tmp_path))
    try:
        f = tmp_path / "busy.txt"
        f.write_text("1")
        for _ in range(5):
            time.sleep(0.05)
            f.write_text("x")
        time.sleep(0.5)
        # A create + rapid modifies within one 1s window must coalesce.
        assert wait_for(lambda: len(events) >= 1)
        time.sleep(0.3)
        assert len(events) <= 2, f"expected coalesced events, got {len(events)}"
    finally:
        w.stop_watching()


def test_throttle_map_pruned(tmp_path):
    """Stale throttle entries must be pruned so the map cannot grow (H-19)."""
    w = FileEventBase(event_throttle_seconds=0.01)
    # Simulate: fire old entries by writing directly into the private map
    import time as _t

    w._last_event_times["stale1"] = _t.time() - 1000
    w._last_event_times["stale2"] = _t.time() - 1000
    w._should_throttle("fresh")
    assert "stale1" not in w._last_event_times
    assert "stale2" not in w._last_event_times


def test_raising_hook_does_not_kill_observer(tmp_path):
    """C-04: a raising hook must not terminate the watchdog observer."""
    boomed = []

    class W(FileEventBase):
        def on_file_created(self, event):
            boomed.append(event)
            raise ValueError("boom")

        def on_error(self, error, event=None):
            boomed.append(("error", str(error)))

    w = W()
    w.start_watching(str(tmp_path))
    observer = w._observer
    try:
        (tmp_path / "boom.txt").write_text("x")
        assert wait_for(lambda: len(boomed) >= 1)
        time.sleep(0.3)
        assert observer.is_alive()  # observer survived the exception
    finally:
        w.stop_watching()


def test_start_validates_path():
    w = FileEventBase()
    with pytest.raises(ConfigurationException, match="does not exist"):
        w.start_watching("/definitely/not/here")
    assert w._watching is False  # H-10 rollback


def test_start_rejects_file_not_directory(tmp_path):
    f = tmp_path / "afile.txt"
    f.write_text("x")
    w = FileEventBase()
    with pytest.raises(ConfigurationException, match="Not a directory"):
        w.start_watching(str(f))
    assert w._watching is False


def test_stop_joins_within_timeout(tmp_path):
    w = FileEventBase()
    w.start_watching(str(tmp_path))
    start = time.time()
    w.stop_watching()
    assert time.time() - start < 5.0


def test_double_start_raises(tmp_path):
    w = FileEventBase()
    w.start_watching(str(tmp_path))
    try:
        with pytest.raises(Exception, match="Already watching"):
            w.start_watching(str(tmp_path))
    finally:
        w.stop_watching()


def test_restart_resets_state(tmp_path):
    w = FileEventBase()
    w.start_watching(str(tmp_path))
    w.stop_watching()
    w.start_watching(str(tmp_path))
    assert w._watching is True
    assert w._observer.is_alive()
    w.stop_watching()
