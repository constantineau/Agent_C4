"""Off-boat backfill (pi/archiver/backfill.py): what leaves the boat, and that none of it is lost.

The backfill is the only path the full-resolution race record takes to the cloud. A regression in
its cursor loses race data with nothing to show for it on shore, and `telemetry_raw` has no
primary key, so re-sending is not free either. Pins, against a temp archive + engine DB with the
cloud POST faked:

  * session mode sends the race and never the day sail; the cursor ends at the top id;
  * a re-run sends nothing (no readings, no sail-log entries, no session markers);
  * a POST that fails half-way loses nothing and duplicates nothing on the re-run;
  * new rows in a later race go up on the next run, and only they do;
  * an open session: rows go up, its marker waits until it is closed;
  * Jul 18: a marker stopped an hour in still sends the hours after it (the derived window);
  * --since/--until sends the window and leaves the cursor alone; --all sends the rest;
  * engine DB unreadable / no sessions: nothing sent, the cursor stays put;
  * the sail log and session markers: in-session only, each once, the right shape.

Run (the archiver image has the deps):  PYTHONPATH=pi/archiver:. python3 pi/archiver/test_backfill.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

TMP = tempfile.mkdtemp()
os.environ["ARCHIVE_DB"] = os.path.join(TMP, "archive.db")
os.environ["ENGINE_DB"] = os.path.join(TMP, "engine.db")
os.environ["BOAT_ID"] = "sr33"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import archiver   # noqa: E402
import backfill   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


SOG, COG = "navigation.speedOverGround", "navigation.courseOverGroundTrue"
GPS = "n2k-socketcan.3"
NOW = time.time()
DAY = NOW - 3 * 86400                 # a day sail three days ago (no session)
RACE = NOW - 2 * 86400                # a race two days ago
RACE_END = RACE + 2 * 3600


# --- fixture ------------------------------------------------------------------------------
class Cloud:
    """The fake /ingest/raw: records every accepted batch; can be told to fail the Nth call."""

    def __init__(self):
        self.batches, self.calls, self.fail_on = [], 0, None

    def __call__(self, readings):
        self.calls += 1
        if self.fail_on == self.calls:
            raise OSError("link dropped")
        self.batches.append(list(readings))

    def by_path(self, path):
        return [r for b in self.batches for r in b if r["path"] == path]

    def telemetry(self):
        return [r for b in self.batches for r in b if not r["path"].startswith("crew.")]


def fresh():
    for p in (os.environ["ARCHIVE_DB"], os.environ["ENGINE_DB"]):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(p + suffix):
                os.remove(p + suffix)
    arc = archiver.open_db(archiver.ARCHIVE_DB)
    ec = sqlite3.connect(os.environ["ENGINE_DB"])
    ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, race_id TEXT, "
               "kind TEXT, start_ts REAL, end_ts REAL)")
    ec.execute("CREATE TABLE sail_log (ts REAL, flying TEXT, reef INTEGER, out_of_service TEXT)")
    ec.commit()
    ec.close()
    archiver._DERIVED_CACHE.clear()
    return arc


def sail(t0, minutes, step=60, sog=3.09, label=None):
    """Under way at 6 kn on 020 for `minutes`: one SOG + one COG row a minute, plus a string row."""
    rows = []
    for k in range(0, minutes * 60 + 1, step):
        rows.append((iso(t0 + k), "sr33", GPS, SOG, sog, None))
        rows.append((iso(t0 + k), "sr33", GPS, COG, 0.349, None))
    if label:
        rows.append((iso(t0 + 30), "sr33", "signalk", "steering.autopilot.state", None, label))
    return rows


def add_rows(arc, rows):
    arc.executemany("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows)
    arc.commit()


def add_session(start, end, name="race"):
    ec = sqlite3.connect(os.environ["ENGINE_DB"])
    cur = ec.execute("INSERT INTO sessions (name, race_id, kind, start_ts, end_ts) "
                     "VALUES (?, 'r1', 'race', ?, ?)", (name, start, end))
    ec.commit()
    sid = cur.lastrowid
    ec.close()
    return sid


def close_session(sid, end):
    ec = sqlite3.connect(os.environ["ENGINE_DB"])
    ec.execute("UPDATE sessions SET end_ts=? WHERE id=?", (end, sid))
    ec.commit()
    ec.close()


def add_sail(ts, flying, reef=0, oos=()):
    ec = sqlite3.connect(os.environ["ENGINE_DB"])
    ec.execute("INSERT INTO sail_log VALUES (?, ?, ?, ?)",
               (ts, json.dumps(list(flying)), reef, json.dumps(list(oos))))
    ec.commit()
    ec.close()


def run(*argv, cloud):
    backfill._post = cloud
    sys.argv = ["backfill.py", *argv]
    backfill.main()


def ids_sent(arc, cloud):
    """Map the sent readings back to archive ids (time+path+value is unique in these fixtures)."""
    key = {(t, p, v, s): i for i, t, p, v, s in
           arc.execute("SELECT id, time, path, value, str_value FROM readings")}
    return [key[(r["time"], r["path"], r.get("value"), r.get("str_value"))]
            for r in cloud.telemetry()]


def in_window(arc, a, b):
    return [i for (i,) in arc.execute("SELECT id FROM readings WHERE time >= ? AND time <= ? "
                                      "ORDER BY id", (iso(a), iso(b)))]


def top_id(arc):
    return arc.execute("SELECT max(id) FROM readings").fetchone()[0]


backfill.BATCH = 50

# --- 1. session mode: the race goes, the day sail stays ------------------------------------
print("session mode — the race goes up, the day sail never leaves the boat:")
arc = fresh()
add_rows(arc, sail(DAY, 60, label="day"))
add_rows(arc, sail(RACE, 120, label="race"))
sid = add_session(RACE, RACE_END)
add_sail(DAY + 60, ["Main", "J1"])                       # day sail: stays
add_sail(RACE + 120, ["Main", "J2"], reef=1, oos=["A3"])  # in the race: goes
add_sail(RACE + 3600, ["Main", "A2"])
cloud = Cloud()
run(cloud=cloud)
sent = ids_sent(arc, cloud)
race_ids = in_window(arc, RACE, RACE_END)
check(f"every race row sent ({len(sent)} of {len(race_ids)})", sorted(sent) == race_ids)
check("no day-sail row sent", not set(sent) & set(in_window(arc, DAY, DAY + 3600)))
check("each row exactly once", len(sent) == len(set(sent)))
check("sent in batches of BACKFILL_BATCH", max(len(b) for b in cloud.batches) <= 50
      and len(cloud.batches) > 2)
check("cursor = the archive's top id (the day sail is passed over for good)",
      backfill.get_cursor(arc) == top_id(arc))
autop = [r for r in cloud.telemetry() if r["path"] == "steering.autopilot.state"]
check("a string reading keeps str_value and has no value key",
      autop == [{"time": iso(RACE + 30), "source": "signalk", "path": "steering.autopilot.state",
                 "str_value": "race"}])
num = cloud.telemetry()[0]
check("a numeric reading has value and no str_value key",
      set(num) == {"time", "source", "path", "value"})
logs = cloud.by_path("crew.sail.state")
check("sail log: only the two in-race changes", len(logs) == 2)
check("sail log: the shape the debrief reads",
      logs and json.loads(logs[0]["str_value"]) == {"flying": ["Main", "J2"], "reef": 1,
                                                    "out_of_service": ["A3"]}
      and logs[0]["source"] == "crew"
      and logs[0]["time"].startswith(iso(RACE + 120)[:19]))
marks = cloud.by_path("crew.session")
check("one session marker for the closed race",
      len(marks) == 1 and json.loads(marks[0]["str_value"])["id"] == sid
      and json.loads(marks[0]["str_value"])["start_ts"] == RACE
      and json.loads(marks[0]["str_value"])["end_ts"] == RACE_END)

print("re-run — nothing new, nothing sent:")
again = Cloud()
run(cloud=again)
check("no POST at all on a re-run", again.calls == 0)

# --- 2. a failed POST half-way --------------------------------------------------------------
print("a POST that fails half-way loses nothing and duplicates nothing:")
arc = fresh()
add_rows(arc, sail(RACE, 120))
add_session(RACE, RACE_END)
c1 = Cloud()
c1.fail_on = 3                                   # two batches accepted, the third drops
try:
    run(cloud=c1)
    raised = False
except OSError:
    raised = True
check("the failure surfaces (the run does not report success)", raised)
first = ids_sent(arc, c1)
check("cursor = the last ACCEPTED row, not the last one read",
      backfill.get_cursor(arc) == max(first) and len(first) == 100)
c2 = Cloud()
run(cloud=c2)
second = ids_sent(arc, c2)
race_ids = in_window(arc, RACE, RACE_END)
check(f"the re-run sends exactly the rest ({len(first)} + {len(second)} = {len(race_ids)})",
      sorted(first + second) == race_ids)
check("…with no row sent twice", not set(first) & set(second))

# --- 3. a later race, then an open session --------------------------------------------------
print("a later race: only its rows go up on the next run:")
RACE2 = NOW - 86400
add_rows(arc, sail(RACE2, 30))
sid2 = add_session(RACE2, RACE2 + 1800, name="race 2")
c3 = Cloud()
run(cloud=c3)
check("only race 2's rows", sorted(ids_sent(arc, c3)) == in_window(arc, RACE2, RACE2 + 1800))
check("only race 2's marker", [json.loads(m["str_value"])["id"]
                               for m in c3.by_path("crew.session")] == [sid2])

print("an open session: its rows go up now, its marker once it is closed:")
OPEN = NOW - 1200
sid3 = add_session(OPEN, None, name="live")
add_rows(arc, sail(OPEN, 10))
add_sail(OPEN + 60, ["Main", "J1"])
c4 = Cloud()
run(cloud=c4)
check("rows of the open session sent", sorted(ids_sent(arc, c4)) == in_window(arc, OPEN, NOW))
check("its sail change sent (an open session counts)", len(c4.by_path("crew.sail.state")) == 1)
check("no marker yet for the open session", c4.by_path("crew.session") == [])
close_session(sid3, OPEN + 900)
c5 = Cloud()
run(cloud=c5)
check("closed: its marker goes, alone", [json.loads(m["str_value"])["id"]
                                         for m in c5.by_path("crew.session")] == [sid3])
check("closed: nothing re-sent", c5.telemetry() == [] and c5.by_path("crew.sail.state") == [])

# --- 4. Jul 18: the button stopped an hour into the race ------------------------------------
print("Jul 18 — a marker stopped an hour in still sends the hours after it:")
arc = fresh()
J_START = NOW - 30 * 86400
J_MARK = J_START + 3600
J_END = J_START + 5 * 3600
add_rows(arc, sail(J_START, 300))
add_session(J_START, J_MARK)
cj = Cloud()
run(cloud=cj)
after = [i for i in ids_sent(arc, cj) if i in set(in_window(arc, J_MARK + 1, J_END))]
check(f"the four hours after the button are sent ({len(after)} rows)",
      len(after) == len(in_window(arc, J_MARK + 1, J_END)) and len(after) > 400)

# --- 5. modes and fail-safes ----------------------------------------------------------------
print("--since/--until: the window, and the cursor untouched:")
arc = fresh()
add_rows(arc, sail(DAY, 60))
add_rows(arc, sail(RACE, 60))
add_session(RACE, RACE + 3600)
c6 = Cloud()
run("--since", iso(DAY - 1)[:19] + "Z", "--until", iso(DAY + 1800)[:19] + "Z", cloud=c6)
check("sends the day-sail window even though no session covers it",
      sorted(ids_sent(arc, c6)) == in_window(arc, DAY, DAY + 1800))
check("cursor untouched by an ad-hoc export", backfill.get_cursor(arc) == 0)
check("no sail log or markers on an ad-hoc export",
      c6.by_path("crew.session") == [] and c6.by_path("crew.sail.state") == [])

print("engine DB unreadable: nothing sent, the cursor stays:")
os.remove(os.environ["ENGINE_DB"])
c7 = Cloud()
run(cloud=c7)
check("nothing sent", c7.calls == 0)
check("cursor still 0 (the race is not skipped for good)", backfill.get_cursor(arc) == 0)

print("no sessions recorded: nothing sent, the cursor stays:")
ec = sqlite3.connect(os.environ["ENGINE_DB"])
ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, race_id TEXT, "
           "kind TEXT, start_ts REAL, end_ts REAL)")
ec.execute("CREATE TABLE sail_log (ts REAL, flying TEXT, reef INTEGER, out_of_service TEXT)")
ec.commit()
ec.close()
c8 = Cloud()
run(cloud=c8)
check("nothing sent", c8.calls == 0 and backfill.get_cursor(arc) == 0)
print("…and once the race's session exists, the next run sends it:")
add_session(RACE, RACE + 3600)
c9 = Cloud()
run(cloud=c9)
check("the race goes up", sorted(ids_sent(arc, c9)) == in_window(arc, RACE, RACE + 3600))

print("--all: everything past the cursor, session or not:")
# The session run above moved the cursor past the day sail on purpose: --all does NOT bring it
# back (that takes --reset or --since). What --all adds is the rows no session covers since.
DELIVERY = NOW - 3600
add_rows(arc, sail(DELIVERY, 20))
c10 = Cloud()
run("--all", cloud=c10)
check("the delivery home (no session) goes up, nothing earlier is re-sent",
      sorted(ids_sent(arc, c10)) == in_window(arc, DELIVERY, DELIVERY + 1200))
check("cursor at the top", backfill.get_cursor(arc) == top_id(arc))

print("--reset: back to the start:")
c11 = Cloud()
run("--reset", "--all", cloud=c11)
check("everything sent again", len(ids_sent(arc, c11)) == top_id(arc))

print("\n" + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
