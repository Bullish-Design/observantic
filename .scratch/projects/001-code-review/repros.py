# Reproduction scripts for review #001

Run inside the project with a live Postgres reachable at `postgresql://postgres@127.0.0.1:5544/eventic_test`
(any URL works for the Record/dispatch tests). Each script is self-contained and
prints the outcome that backs the finding.

## 1. C-02 — README Quick Start crashes at class definition

```python
from observantic import FileEventBase, init
from eventic import Record

init(name="my-app", database_url="postgresql://postgres@127.0.0.1:5544/eventic_test")

class FileEvent(Record, FileEventBase):
    path: str = "/tmp/qs"
    event_type: str = "created"
    watch_patterns = ["*.pdf", "*.txt"]   # PydanticUserError: unannotated override
```

## 2. C-01 — SQLite monitor detects nothing (data_version gate)

```python
import sqlite3, os, time, warnings
warnings.filterwarnings("ignore")
from observantic import SQLiteEventBase

p = "/tmp/sqltest.db"
if os.path.exists(p): os.unlink(p)
c = sqlite3.connect(p)
c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT)")
c.commit(); c.close()

seen = []
class SW(SQLiteEventBase):
    def on_data_changed(self, db_path, new_rows):
        seen.append([r.row_id for r in new_rows])

w = SW()
w.start_watching(p)
time.sleep(0.4)
c = sqlite3.connect(p)
c.execute("INSERT INTO t (data) VALUES ('b')"); c.commit(); c.close()
time.sleep(1.2)
print("detected rows:", seen)          # [] — nothing detected
w.stop_watching()
```

With the gate disabled it works:

```python
w._last_data_version = None   # disable the stale gate → rows detected
```

## 3. C-03 — Record-based dispatch raises DBOSException without launch()

```python
import warnings; warnings.filterwarnings("ignore")
from eventic import Record
from observantic import FileEventBase

class TW(Record, FileEventBase):
    path: str = "x"
    def on_file_created(self, event):
        print("hook ran:", event)

w = TW()
w._dispatch_hook("on_file_created", "EVENT")
# → DBOSException: DBOS Error: No DBOS was created yet   (enqueue inside evented())
```

## 4. C-04 — raising hook kills the watchdog observer thread

```python
import time, warnings; warnings.filterwarnings("ignore")
from observantic import FileEventBase

class W(FileEventBase):
    def on_file_created(self, event):
        raise ValueError("boom")

w = W()
w.start_watching("/tmp")          # watch any existing dir
time.sleep(0.3)
open("/tmp/repro-boom.txt", "w").close()
time.sleep(0.5)
print("observer alive:", w._observer.is_alive())   # False — thread died
w.stop_watching()
```

## 5. C-05 — webhook DoS and hanging stop_watching()

```python
import socket, threading, time, warnings; warnings.filterwarnings("ignore")
from observantic import WebhookEventBase

w = WebhookEventBase(port=18997, webhook_paths=["/hook"])
w.start_watching(); time.sleep(0.4)

att = socket.create_connection(("127.0.0.1", 18997), timeout=3)
att.sendall(b"POST /hook HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999\r\n\r\n")
time.sleep(0.5)

s2 = socket.create_connection(("127.0.0.1", 18997), timeout=3)
s2.sendall(b"POST /hook HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n")
s2.settimeout(4)
try:
    s2.recv(4096); print("unexpected: got response")
except socket.timeout:
    print("second request HUNG — single-threaded server blocked")

done = []
def stop(): w.stop_watching(); done.append(True)
t = threading.Thread(target=stop); t.start(); t.join(timeout=5)
print("stop_watching returned in 5s:", bool(done))   # False — hangs
att.close(); s2.close()
```

## 6. C-06 — invalid Content-Length → unhandled ValueError

```python
import socket, time, warnings; warnings.filterwarnings("ignore")
from observantic import WebhookEventBase

w = WebhookEventBase(port=18998, webhook_paths=["/hook"])
w.start_watching(); time.sleep(0.4)
s = socket.create_connection(("127.0.0.1", 18998), timeout=3)
s.sendall(b"POST /hook HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n{}")
s.settimeout(2)
try:
    print(s.recv(4096))
except socket.timeout:
    print("no response — ValueError killed the connection (see stderr traceback)")
s.close(); w.stop_watching()
```

## 7. C-07 — class-level field assignment is a no-op

```python
from pathlib import Path
from eventic import Record
from observantic import WebhookEventBase

class WebhookLogger(Record, WebhookEventBase):
    endpoint: str = "/webhook"
    payload: dict | str = {}
    timestamp: float = 0.0
    port: int = 8000
    _log_file: Path = Path("/data/webhooks.jsonl")

WebhookLogger.port = 9999
inst = WebhookLogger()
print("port:", inst.port)   # 8000 — option ignored
```

## 8. H-13 — OBSERVANTIC_DB_URL env var does nothing

```python
import os, warnings; warnings.filterwarnings("ignore")
os.environ["OBSERVANTIC_DB_URL"] = "postgresql://custom"
from observantic.config import ObservanticSettings
print(ObservanticSettings().DB_URL)   # postgresql://localhost/observantic — unchanged
```

## 9. C-09 — full test suite

```bash
uv sync
uv pip install -e .
uv add --dev pytest
pytest src/tests/tests.py -q          # 7 failed
```
(Requires a reachable Postgres, or the fixture errors out first — either way red.)
