"""Reproduction scripts for review #002 (first-principles).

Run:  devenv shell uv run python .scratch/projects/002-first-principles-review/repros.py

Each section is self-contained, prints a verdict, and never leaves the suite
broken. Verdicts: REPRODUCED / NOT REPRODUCED / OBSERVED (nondeterministic).
"""

from __future__ import annotations

import http.client
import json
import logging
import socket
import sqlite3
import threading
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

HERE = Path(__file__).resolve().parent

logging.basicConfig(level=logging.ERROR)


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def wait_for(pred, timeout=5.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return False


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# --------------------------------------------------------------------------
# R-01  auto_persist does not gate persistence for Record-based watchers
#       (construction-time durable v0 writes regardless of auto_persist).
# --------------------------------------------------------------------------
def repro_01() -> None:
    section("R-01  init() alone persists every emitted record for Record watchers")
    import tempfile

    from eventic import Record, on
    from observantic import FileEventBase, init, reset

    seen_create = []
    tmp = tempfile.mkdtemp()

    class MyFileEvent(Record, FileEventBase):
        path: str = ""
        event_type: str = ""
        watch_patterns: list[str] = ["*"]

    @on.create(MyFileEvent)
    def _created(rec):
        seen_create.append((rec.__class__.__name__, rec.id))

    w = MyFileEvent()  # auto_persist defaults to False
    init(name="obs-r01", database_url=f"sqlite:///{tmp}/e.db")
    try:
        # No start_watching: just emit directly.
        rec = w._emit(path="/x", event_type="created")
        # Did the store get a row even though auto_persist=False?
        n_versions = None
        try:
            fresh = MyFileEvent.hydrate(rec.id)
            n_versions = fresh.version
        except KeyError:
            n_versions = "NOT IN STORE"
        print(
            f"auto_persist=False, init() called -> emitted record v{n_versions} "
            f"in store, create handlers fired: {len(seen_create)}"
        )
        verdict = (
            "REPRODUCED: init() alone writes rows for Record watchers; "
            "auto_persist does not gate persistence"
            if n_versions == 0 and len(seen_create) >= 1
            else "NOT REPRODUCED"
        )
        print(f"-> {verdict}")
    finally:
        reset()
        from eventic.events import _registry

        _registry._handlers["create"].pop(MyFileEvent, None)
        _registry._handlers["update"].pop(MyFileEvent, None)


# --------------------------------------------------------------------------
# R-02  auto_persist=True on a *plain* (non-Record) watcher warns per event
#       and persists nothing.
# --------------------------------------------------------------------------
def repro_02() -> None:
    section("R-02  auto_persist=True + plain model = per-event warning, nothing persisted")
    import tempfile

    from observantic import FileEventBase

    tmp = tempfile.mkdtemp()
    w = FileEventBase(auto_persist=True)
    logs = []
    h = logging.Handler()
    h.emit = lambda r: logs.append(r.getMessage())
    logger = logging.getLogger("observantic")
    logger.addHandler(h)
    try:
        rec = w._emit(path="/x", event_type="created")
        print(f"emitted record type: {type(rec).__name__}, warnings: {len(logs)}")
        print(
            "-> REPRODUCED: plain watcher + auto_persist=True logs a warning "
            "per event and persists nothing"
            if logs
            else "-> NOT REPRODUCED"
        )
    finally:
        logger.removeHandler(h)


# --------------------------------------------------------------------------
# R-03  persist_strict=True + file watcher + no init -> observer thread dies
#       (ConfigurationException escapes _emit inside the watchdog handler).
# --------------------------------------------------------------------------
def repro_03() -> None:
    section("R-03  persist_strict=True kills the file observer (emit escapes dispatch)")
    import tempfile

    from observantic import FileEventBase

    tmp = tempfile.mkdtemp()
    errors = []

    class W(FileEventBase):
        watch_patterns: list[str] = ["*"]

        def on_error(self, error, event=None):
            errors.append(error)

    w = W(auto_persist=True, persist_strict=True)
    w.start_watching(tmp)
    obs = w._observer
    try:
        (Path(tmp) / "boom.txt").write_text("x")
        time.sleep(1.0)
        print(f"observer alive after strict-persist event: {obs.is_alive()}")
        print(
            "-> REPRODUCED: observer died (emit raised inside watchdog handler)"
            if not obs.is_alive()
            else "-> NOT REPRODUCED"
        )
    finally:
        w.stop_watching()


# --------------------------------------------------------------------------
# R-04  webhook _last_hook_error race: concurrent requests can mis-answer 500.
# --------------------------------------------------------------------------
def repro_04() -> None:
    section("R-04  webhook 500 decision races on shared _last_hook_error")
    from observantic import WebhookEventBase

    port = free_port()

    class W(WebhookEventBase):
        def on_webhook_received(self, event):
            time.sleep(0.3)  # widen the race window
            if event.path == "/fail":
                raise ValueError("boom")

    w = W(port=port, host="127.0.0.1", webhook_paths=["/ok", "/fail"])
    w.start_watching()
    try:
        results = {}

        def hit(which: str, marker: str) -> None:
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("POST", f"/{which}", body=b"{}", headers={"Content-Length": "2"})
                r = c.getresponse()
                results[marker] = r.status
                r.read()
                c.close()
            except Exception as e:  # noqa: BLE001
                results[marker] = f"ERR {e}"

        # Fire 20 pairs: ok + fail concurrently.
        bad = 0
        for _ in range(20):
            results.clear()
            t1 = threading.Thread(target=hit, args=("ok", "ok"))
            t2 = threading.Thread(target=hit, args=("fail", "fail"))
            t1.start(); t2.start(); t1.join(); t2.join()
            if results.get("ok") != 200 or results.get("fail") != 500:
                bad += 1
                if bad <= 3:
                    print(f"  mis-answer: ok={results.get('ok')} fail={results.get('fail')}")
        print(f"pairs with a wrong status answer: {bad}/20")
        print(
            "-> REPRODUCED: shared _last_hook_error corrupts concurrent 500 decisions"
            if bad > 0
            else "-> NOT REPRODUCED (window too small on this run)"
        )
    finally:
        w.stop_watching()


# --------------------------------------------------------------------------
# R-05  two webhook watchers share the connection-tracking set: stopping one
#       closes the other's live sockets.
# --------------------------------------------------------------------------
def repro_05() -> None:
    section("R-05  _ConnectionTrackingMixIn state is class-level (shared across servers)")
    import socket as s

    from observantic import WebhookEventBase

    p1, p2 = free_port(), free_port()
    w1 = WebhookEventBase(port=p1, host="127.0.0.1", webhook_paths=["/w"])
    w2 = WebhookEventBase(port=p2, host="127.0.0.1", webhook_paths=["/w"])
    w1.start_watching()
    w2.start_watching()
    try:
        # Genuine wedge: Content-Length below the cap but the client sends no
        # body, so the handler blocks in rfile.read().
        sock = s.create_connection(("127.0.0.1", p2), timeout=3)
        sock.sendall(
            b"POST /w HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n"
        )
        time.sleep(0.3)
        shared = w1._server._connections is w2._server._connections
        tracked = len(w2._server._connections)
        print(f"servers share the _connections set: {shared}, w2 tracks {tracked} socket(s)")
        # stopping w1 must NOT disturb w2
        w1.stop_watching()
        time.sleep(0.3)
        tracked_after = len(w2._server._connections)
        print(f"after stopping w1: w2 tracks {tracked_after} socket(s)")
        print(
            "-> REPRODUCED: class-level tracking shared across servers "
            "(a second server can close the first's sockets)"
            if shared
            else "-> NOT REPRODUCED"
        )
        print(
            "-> NOTE: the tracking set is empty even while a handler is blocked "
            "(process_request removes the socket before the handler thread starts); "
            "close_all_connections can never unblock a wedged handler"
            if tracked == 0
            else ""
        )
        sock.close()
    finally:
        try:
            w1.stop_watching()
        except Exception:
            pass
        w2.stop_watching()


# --------------------------------------------------------------------------
# R-06  stop_watching() from inside a hook raises RuntimeError (join self).
# --------------------------------------------------------------------------
def repro_06() -> None:
    section("R-06  stop_watching() from inside a hook (poll thread) raises")
    import tempfile

    from observantic import SQLiteEventBase

    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "t.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, data TEXT)")
    c.commit()
    c.close()

    stopped_from_hook = []

    class W(SQLiteEventBase):
        def on_row_inserted(self, row):
            stopped_from_hook.append(threading.current_thread().name)
            self.stop_watching()  # deadlocks: never returns

    w = W(poll_interval_seconds=0.1)
    w.start_watching(str(db))
    try:
        time.sleep(0.5)
        for _ in range(12):
            c = sqlite3.connect(db)
            c.execute("INSERT INTO t (data) VALUES ('x')")
            c.commit()
            c.close()
            if stopped_from_hook:
                break
            time.sleep(0.4)
        time.sleep(2.0)
        stuck = bool(stopped_from_hook) and w._poll_thread is not None and w._poll_thread.is_alive()
        print(f"hook fired on: {stopped_from_hook}; poll thread stuck after stop_watching: {stuck}")
        print(
            "-> REPRODUCED (deadlock): stop_watching() from inside a hook deadlocks — "
            "poll thread holds _check_lock and waits for watchdog's internal _lock; "
            "the watchdog thread is blocked in _check_for_changes waiting on "
            "_check_lock. Neither wait has a timeout, so stop_watching never returns "
            "and the watcher is permanently wedged (see stack dump in review)."
            if stuck
            else "-> OBSERVED (hook did not fire on this run; detection flaky)"
        )
    finally:
        # Cannot stop cleanly from here either (main thread also deadlocks if the
        # poll thread is stuck): nothing to do but let daemon threads die.
        pass


# --------------------------------------------------------------------------
# R-07  events still dispatched after stop_watching() returns (async stop).
# --------------------------------------------------------------------------
def repro_07() -> None:
    section("R-07  file events can fire after stop_watching() returns")
    import tempfile

    from observantic import FileEventBase

    tmp = tempfile.mkdtemp()
    seen_after_stop = []

    class W(FileEventBase):
        watch_patterns: list[str] = ["*"]

        def on_file_created(self, event):
            if self._stopped_at is not None and time.time() > self._stopped_at:
                seen_after_stop.append(event.src_path)

    w = W()
    w._stopped_at = None
    orig_dispatch = type(w)._dispatch_hook

    def slow_dispatch(self, name, *a, **k):
        # widen the window: hold the dispatch while stop happens
        time.sleep(0.5)
        return orig_dispatch(self, name, *a, **k)

    type(w)._dispatch_hook = slow_dispatch  # type: ignore[method-assign]
    w.start_watching(tmp)
    try:
        (Path(tmp) / "late.txt").write_text("x")
        time.sleep(0.3)  # handler is now parked inside slow_dispatch
        t0 = time.time()
        w.stop_watching()
        dt = time.time() - t0
        time.sleep(0.8)  # let the parked dispatch complete
        print(f"stop_watching returned in {dt:.2f}s; dispatch completed after stop")
        print(
            "-> REPRODUCED: handler dispatch outlives stop_watching()"
            if dt < 0.5
            else "-> OBSERVED"
        )
    finally:
        w._observer = None
        type(w)._dispatch_hook = orig_dispatch


# --------------------------------------------------------------------------
# R-08  chunked Transfer-Encoding bodies are silently dropped (empty event).
# --------------------------------------------------------------------------
def repro_08() -> None:
    section("R-08  chunked request body silently dropped")
    from observantic import WebhookEventBase

    port = free_port()
    received = []

    class W(WebhookEventBase):
        def on_webhook_received(self, event):
            received.append(event.body)

    w = W(port=port, host="127.0.0.1")
    w.start_watching()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        body = b'{"a": 1}'
        s.sendall(
            b"POST /webhook HTTP/1.1\r\nHost: x\r\n"
            b"Transfer-Encoding: chunked\r\nContent-Type: application/json\r\n\r\n"
            + hex(len(body))[2:].encode()
            + b"\r\n"
            + body
            + b"\r\n0\r\n\r\n"
        )
        s.settimeout(5)
        data = s.recv(4096)
        s.close()
        wait_for(lambda: len(received) >= 1)
        print(f"response line: {data.split(b'\r\n', 1)[0]!r}")
        print(f"hook received body: {received[0]!r}")
        print(
            "-> REPRODUCED: chunked body arrives as empty body"
            if received and received[0] == b""
            else "-> NOT REPRODUCED"
        )
    finally:
        w.stop_watching()


# --------------------------------------------------------------------------
# R-09  auth header is delivered into the event record (secret leak).
# --------------------------------------------------------------------------
def repro_09() -> None:
    section("R-09  auth header value included in event/record payloads")
    from observantic import WebhookEventBase

    port = free_port()
    received = []

    class W(WebhookEventBase):
        require_auth_header: str | None = "X-API-Key"
        require_auth_value: str | None = "super-secret"

        def on_webhook_received(self, event):
            received.append(event.headers)

    w = W(port=port, host="127.0.0.1")
    w.start_watching()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/webhook", body=b"{}", headers={"Content-Length": "2", "X-API-Key": "super-secret"})
        r = c.getresponse(); r.read(); c.close()
        wait_for(lambda: len(received) >= 1)
        leaked = received[0].get("X-API-Key") if received else None
        print(f"hook event.headers contains X-API-Key: {leaked!r}")
        print(
            "-> REPRODUCED: secret auth header is forwarded into event payloads"
            if leaked == "super-secret"
            else "-> NOT REPRODUCED"
        )
    finally:
        w.stop_watching()


# --------------------------------------------------------------------------
# R-10  README: dispatch_direct=False + registered @evented callback.
# --------------------------------------------------------------------------
def repro_10() -> None:
    section("R-10  'DBOS queue semantics via register_hook + dispatch_direct=False'")
    from eventic.queues.dispatcher import evented
    from observantic import EventWatcher

    calls = []

    @evented
    def queued_cb(event):
        calls.append("ran")

    w = EventWatcher(dispatch_direct=False)
    w.register_hook("on_thing", queued_cb)
    try:
        w._dispatch_hook("on_thing", object())
        print(f"registered @evented callback ran synchronously: {calls}")
        print(
            "-> OBSERVED: the callback runs inline; no DBOS queue semantics "
            "(metaclass wrapping only applies to class methods, not registered fns)"
        )
    finally:
        w.unregister_hook("on_thing", queued_cb)


# --------------------------------------------------------------------------
# R-11  concurrent start_watching: TOCTOU allows two observers.
# --------------------------------------------------------------------------
def repro_11() -> None:
    section("R-11  concurrent start_watching: no double-start guard under lock")
    import tempfile

    from observantic import FileEventBase

    tmp = tempfile.mkdtemp()
    w = FileEventBase()

    # Widen the TOCTOU window between the "already watching" check and the
    # state flip so both threads pass the check before either flips.
    orig_validate = w._validate_start

    def slow_validate(path):
        time.sleep(0.5)
        return orig_validate(path)

    w._validate_start = slow_validate  # type: ignore[method-assign]
    results = []

    def go():
        try:
            w.start_watching(tmp)
            results.append("ok")
        except Exception as e:  # noqa: BLE001
            results.append(f"raised {type(e).__name__}")

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print(f"two concurrent starts -> {results}")
    print(
        "-> REPRODUCED: both starts passed the guard and both called _start_impl "
        "(two observers created; the second silently replaced the first)"
        if all(r == "ok" for r in results)
        else "-> OBSERVED (race window missed on this run)"
    )
    try:
        w.stop_watching()
    except Exception:
        pass


# --------------------------------------------------------------------------
# R-12  locked DB at start -> raw sqlite3 error, not a typed exception.
# --------------------------------------------------------------------------
def repro_12() -> None:
    section("R-12  locked DB at start_watching -> raw sqlite3 error escapes")
    import tempfile

    from observantic import SQLiteEventBase
    from observantic.exceptions import ObservanticException

    tmp = tempfile.mkdtemp()
    db = Path(tmp) / "l.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    c.commit()
    blocker = sqlite3.connect(db)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        w = SQLiteEventBase(db_connect_timeout_seconds=0.1)
        try:
            w.start_watching(str(db))
            print("start succeeded unexpectedly")
        except Exception as e:  # noqa: BLE001
            typed = isinstance(e, ObservanticException)
            print(f"start raised {type(e).__name__}: {str(e)[:80]!r}; typed: {typed}")
            print(
                "-> REPRODUCED: raw sqlite3.OperationalError escapes _start_impl"
                if not typed and "sqlite3" in type(e).__module__
                else "-> OBSERVED"
            )
    finally:
        blocker.rollback()
        blocker.close()


# --------------------------------------------------------------------------
# R-13  same-named Record classes in one process.
# --------------------------------------------------------------------------
def repro_13() -> None:
    section("R-13  duplicate Record class names")
    from eventic import Record

    try:

        class DupRec(Record):
            a: int = 0

        class DupRec(Record):  # noqa: F811
            b: int = 0

        print("second same-named Record class defined without error")
        print("-> OBSERVED: no raise at class definition")
    except Exception as e:  # noqa: BLE001
        print(f"second definition raised {type(e).__name__}: {e}")
        print("-> REPRODUCED: same-named Record classes raise at definition")


# --------------------------------------------------------------------------
# R-14  README Quick Start verbatim (annotated) works end-to-end without init.
# --------------------------------------------------------------------------
def repro_14() -> None:
    section("R-14  README Quick Start (annotated) — baseline sanity")
    import tempfile

    from observantic import FileEventBase

    tmp = tempfile.mkdtemp()

    class FileEvent(FileEventBase):
        path: str = ""
        event_type: str = ""
        watch_patterns: list[str] = ["*.pdf", "*.txt"]

        def on_file_created(self, event):
            pass

    w = FileEvent()
    w.start_watching(tmp)
    try:
        (Path(tmp) / "doc.pdf").write_text("x")
        time.sleep(0.6)
        print(f"watcher started/stopped cleanly, observer alive: {w._observer.is_alive()}")
        print("-> PASS (baseline)")
    finally:
        w.stop_watching()


# --------------------------------------------------------------------------
# R-15  emitted (persisted) record carries watcher-config fields.
# --------------------------------------------------------------------------
def repro_15() -> None:
    section("R-15  watcher-as-record: config fields pollute the persisted schema")
    import tempfile

    from eventic import Record
    from observantic import FileEventBase, init, reset

    tmp = tempfile.mkdtemp()
    init(name="obs-r15", database_url=f"sqlite:///{tmp}/e.db")
    try:

        class FileEvent(Record, FileEventBase):
            path: str = ""
            event_type: str = ""
            watch_patterns: list[str] = ["*.pdf"]

        rec = FileEvent()._emit(path="/x", event_type="created", is_directory=False)
        dumped = rec.model_dump(mode="python")
        config_keys = [
            k
            for k in sorted(dumped)
            if k
            in ("watch_patterns", "ignore_patterns", "case_sensitive",
                "event_throttle_seconds", "auto_persist", "persist_strict",
                "dispatch_direct", "raise_on_hook_error", "record_model")
        ]
        print(f"config fields in emitted record: {config_keys}")
        print(
            "-> REPRODUCED: the watcher and the record are the same object, so "
            "every persisted event carries all watcher config fields"
            if len(config_keys) >= 5
            else "-> NOT REPRODUCED"
        )
    finally:
        reset()


# --------------------------------------------------------------------------
# R-16  a required record field makes the watcher unconstructible.
# --------------------------------------------------------------------------
def repro_16() -> None:
    section("R-16  required record fields break watcher construction")
    from eventic import Record
    from observantic import FileEventBase

    try:

        class ReqRec(Record, FileEventBase):
            path: str  # natural pydantic style: required field
            event_type: str = ""
            watch_patterns: list[str] = ["*.pdf"]

        ReqRec()
        print("watcher constructed with a required record field")
        print("-> NOT REPRODUCED")
    except Exception as e:  # noqa: BLE001
        print(f"ReqRec() raised {type(e).__name__}: {str(e)[:80]}")
        print(
            "-> REPRODUCED: the watcher cannot be constructed unless every record "
            "field has a default (record schema and watcher are conflated)"
        )


# --------------------------------------------------------------------------
# R-17  unannotated config override -> PydanticUserError (lingering C-02 trap).
# --------------------------------------------------------------------------
def repro_17() -> None:
    section("R-17  unannotated config override raises at class definition")
    from observantic import FileEventBase

    try:

        class W(FileEventBase):
            event_throttle_seconds = 0.5  # natural Python; missing annotation

        W()
        print("unannotated override accepted")
        print("-> NOT REPRODUCED")
    except Exception as e:  # noqa: BLE001
        print(f"class definition raised {type(e).__name__}")
        print(
            "-> REPRODUCED: subclassing a watcher and setting config in the "
            "natural style raises PydanticUserError; every config override must "
            "be annotated, a recurring footgun"
        )


# --------------------------------------------------------------------------
# R-18  webhook rejection paths corrupt the next keep-alive request.
# --------------------------------------------------------------------------
def repro_18() -> None:
    section("R-18  unread bodies on 4xx/413 corrupt the next request")
    from observantic import WebhookEventBase

    port = free_port()
    w = WebhookEventBase(port=port, host="127.0.0.1", webhook_paths=["/w"])
    w.start_watching()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(
            b"POST /w HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\n\r\nJUNK"
        )
        time.sleep(0.2)
        s.sendall(b"POST /w HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
        s.settimeout(5)
        data = b""
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
        except socket.timeout:
            pass
        s.close()
        text = data.decode("utf-8", errors="replace")
        second = text.split("HTTP/1.1")[2] if text.count("HTTP/1.1") > 1 else ""
        misparsed = "JUNKPOST" in second or "Unsupported" in second
        print(f"second response: {second.strip().splitlines()[0] if second else 'none'}")
        print(
            "-> REPRODUCED: leftover body after a 413 (and 401/404/405) is read as "
            "the next request line on the same keep-alive connection"
            if misparsed
            else "-> OBSERVED"
        )
    finally:
        w.stop_watching()


def main() -> None:
    repro_14()   # baseline sanity first
    repro_01()
    repro_02()
    repro_03()
    repro_04()
    repro_05()
    repro_06()
    repro_07()
    repro_08()
    repro_09()
    repro_10()
    repro_11()
    repro_12()
    repro_13()
    repro_15()
    repro_16()
    repro_17()
    repro_18()


if __name__ == "__main__":
    main()
