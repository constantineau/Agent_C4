"""Ingestion refuses data that doesn't say which boat it's from (#307).

Before: a batch without `boat_id` was filed under "sr33", so an uplink that left it out wrote
into sr33's history. Now it's a 400 with a plain message, and nothing is written.
"""
import os
import subprocess
import sys
from contextlib import contextmanager

os.environ.setdefault("INGEST_TOKEN", "test-token")
_no_dsn = subprocess.run([sys.executable, "-c", "import app.main"],
                         env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"},
                         capture_output=True, text=True, timeout=60)
os.environ.setdefault("DATABASE_URL", "postgresql://t:made-up@127.0.0.1:1/t")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

ok = True
WRITES = []


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def executemany(self, sql, rows):
        WRITES.extend(rows)


class _Conn:
    def cursor(self):
        return _Cur()

    def commit(self):
        pass


class _Pool:
    @contextmanager
    def connection(self):
        yield _Conn()

    def open(self, **kw):
        pass

    def close(self):
        pass


main.pool = _Pool()
c = TestClient(main.app)
H = {"Authorization": f"Bearer {main.INGEST_TOKEN}"}
READING = {"time": "2026-11-10T12:00:00Z", "source": "n2k.1", "path": "navigation.speedOverGround",
           "value": 3.2}
TARGET = {"time": "2026-11-10T12:00:00Z", "mmsi": 316000001, "lat": 45.0, "lon": -82.0}

print("start-up:")
check("no DATABASE_URL → won't start, saying so (no built-in DSN)", _no_dsn.returncode != 0
      and "DATABASE_URL is not set" in (_no_dsn.stderr.strip().splitlines() or [""])[-1])

print("/ingest/raw:")
r = c.post("/ingest/raw", json={"readings": [READING]}, headers=H)
check("no boat_id → 400, plain message, nothing written",
      r.status_code == 400 and r.json()["detail"] == main.NO_BOAT and not WRITES)
r = c.post("/ingest/raw", json={"boat_id": "  ", "readings": [READING]}, headers=H)
check("blank boat_id → 400", r.status_code == 400 and not WRITES)
r = c.post("/ingest/raw", json={"boat_id": "bravo", "readings": [READING]}, headers=H)
check("boat_id given → stored under it", r.status_code == 200 and r.json()["boat_id"] == "bravo"
      and [w[1] for w in WRITES] == ["bravo"])

print("/ingest/ais:")
WRITES.clear()
r = c.post("/ingest/ais", json={"targets": [TARGET]}, headers=H)
check("no boat_id → 400, nothing written", r.status_code == 400 and not WRITES)
r = c.post("/ingest/ais", json={"boat_id": "bravo", "targets": [TARGET]}, headers=H)
check("boat_id given → stored under it", r.status_code == 200 and [w[1] for w in WRITES] == ["bravo"])

print("ALL OK" if ok else "FAILURES")
sys.exit(0 if ok else 1)
