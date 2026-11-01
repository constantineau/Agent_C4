"""One unwritable row must never block a batch.

A Signal K string may carry a lone UTF-16 surrogate: valid JSON ("\\ud800"), but not valid
UTF-8, so SQLite's bind raises UnicodeEncodeError. Before, that failed the whole batch, which was
requeued at the FRONT of the buffer and failed again on every flush: nothing was written until
the buffer cap finally dropped the row, ~7-10 min of the full-res record at the dock rate.

Now `flatten` cleans such strings ("?"), and if a batch still fails on a row's values the rows
are written one by one and only the bad one is dropped, with a rate-limited log line.

Run:  python3 pi/archiver/test_bad_rows.py
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.modules.setdefault("websockets", type(sys)("websockets"))
import archiver  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SURR = json.loads('"AIS name \\ud800 here"')     # what json.loads hands the archiver
ROW = ("2026-11-01T12:00:00.000Z", "sr33", "n2k.15", "navigation.speedOverGround", 3.0, None)

print("flatten cleans lone surrogates:")
out = []
archiver.flatten("2026-11-01T12:00:00.000Z", "ais\ud800", "name", SURR, out)
check("one row, still a string", len(out) == 1 and isinstance(out[0][5], str))
check("the surrogate is replaced, the rest kept",
      out[0][5] == "AIS name ? here" and out[0][2] == "ais?")
out[0][5].encode("utf-8")                      # would raise if not cleaned
check("ordinary strings are untouched",
      archiver._clean("Spinnaker — A3 ☀") == "Spinnaker — A3 ☀")


def flush(buf, state, db):
    async def go():
        await archiver._flush_once(buf, asyncio.get_running_loop(), state, db, None)
    asyncio.run(go())


print("a batch with one unwritable row (bypassing flatten): the rest is written at once:")
with tempfile.TemporaryDirectory() as d:
    db = Path(d) / "archive.db"
    conn = archiver.open_db(db)
    state = {"conn": conn}
    bad = ("2026-11-01T12:00:01.000Z", "sr33", "n2k.15", "name", None, SURR)
    buf = [ROW] * 10 + [bad] + [ROW] * 10
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        flush(buf, state, db)
    n = conn.execute("SELECT count(*) FROM readings").fetchone()[0]
    check(f"20 good rows written on the first flush ({n})", n == 20)
    check("nothing requeued (the batch is not stuck)", buf == [])
    check("the bad row is counted", state.get("rows_dropped_bad") == 1)
    check("total counts only what was written", state.get("total") == 20)
    check("and said so", "dropped 1 unwritable row" in log.getvalue())

    print("a stream of bad rows logs once per window:")
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        for _ in range(30):
            buf[:] = [ROW, bad]
            flush(buf, state, db)
    lines = [ln for ln in log.getvalue().splitlines() if "unwritable" in ln]
    check(f"30 bad batches, {len(lines)} log line(s)", len(lines) <= 1)
    check("all 30 good rows still written",
          conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 50)
    st = archiver.write_status(state, None, path=Path(d) / "s.json")
    check("the status file carries the count", st["rows_dropped_unwritable"] == 31)
    conn.close()

print("a lock is NOT a row error: the batch is requeued whole, nothing dropped:")


class Locked:
    def executemany(self, *a):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")

    def execute(self, *a):
        raise AssertionError("must not fall back to row-by-row for a lock")

    def commit(self):
        pass

    def rollback(self):
        pass


with tempfile.TemporaryDirectory() as d:
    state = {"conn": Locked()}
    buf = [ROW] * 5
    with contextlib.redirect_stdout(io.StringIO()):
        flush(buf, state, Path(d) / "archive.db")
    check("all 5 rows back in the buffer", len(buf) == 5 and not state.get("rows_dropped_bad"))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
