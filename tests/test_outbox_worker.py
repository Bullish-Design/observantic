"""Outbox delivery loop: watcher persists -> Outbox subscription -> Worker.

Proves observantic's durable-delivery story end to end on SQLite: an
auto-persisting watcher writes commits, the outbox drains exactly once, and
a second drain is a no-op.
"""

from __future__ import annotations

from eventic import App, Outbox, Stream, Subscription
from eventic.sql import SQLite
from eventic.worker import Worker
from pydantic import BaseModel

from observantic import EventWatcher


class ProbeEvent(BaseModel):
    path: str = ""
    event_type: str = ""


PROBE_STREAM = Stream(ProbeEvent, name="probe")


class EmittingWatcher(EventWatcher):
    stream: Stream | None = PROBE_STREAM


def test_outbox_worker_delivers_persisted_commits(tmp_path):
    seen = []

    def handler(commit):
        seen.append((commit.kind, commit.revision.state.path))

    app = App(
        id="outbox-test",
        streams=[PROBE_STREAM],
        subscriptions=[
            Subscription(
                id="outbox-test.probe",
                stream=PROBE_STREAM,
                handler=handler,
                delivery=Outbox(queue="q"),
            )
        ],
    )
    store = SQLite(str(tmp_path / "events.db"))
    try:
        runtime = app.bind(store)
        w = EmittingWatcher(auto_persist=True)
        w.bind(runtime)
        w._emit(path="/a", event_type="created")
        w._emit(path="/b", event_type="created")

        report = Worker(app, store, queue="q").drain_once()
        assert report.claimed == 2 and report.delivered == 2
        assert seen == [("create", "/a"), ("create", "/b")]

        # second drain is a no-op (intents are claimed, not re-delivered)
        assert Worker(app, store, queue="q").drain_once().claimed == 0
        assert seen == [("create", "/a"), ("create", "/b")]
    finally:
        store.close()
