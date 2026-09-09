"""The retention prune keeps the race the RECORD supports, not the one the button recorded.

`archiver.prune()` deletes every reading older than ARCHIVE_RETAIN_DAYS that falls outside a
race-session window, and until now those windows were exactly the intervals between two taps on
the iPad's ⏺ LOG button. On 2026-07-18 that button was caught during a kite hoist and wrote
`end_ts` 4.2 s into it: the marker claims 1 h 49 m of a 7 h race. Shore-side that is a short
debrief. On the boat it is the DELETION path — the five hours nobody pressed a button for
include the flat house bank, the kicked compass and the retirement.

This test builds that shape in miniature — a marker that stops an hour into a five-hour sail,
all of it older than the retention window — and asserts the rest of the race is still there
afterwards. It also asserts the parts that must NOT change: a real day sail still gets pruned,
an unreadable engine DB still deletes nothing, and a derivation that fails or is missing falls
back to the marker rather than to a wider guess.

Run (needs `websockets`, which the system python lacks — use the archiver image):
  docker run --rm -v $PWD/pi/archiver:/app/pi-archiver:ro -v $PWD/shared:/app/shared:ro \
    -w /app sr33-pi-archiver python pi-archiver/test_prune_window.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import archiver                                                          # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def iso(epoch):
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


NOW = time.time()
RACE_START = NOW - 30 * 86400           # a month ago: everything here is past the 14-day cutoff
MARKER_END = RACE_START + 3600          # the button was caught an hour in
RACE_END = RACE_START + 5 * 3600        # they sailed four more hours
SOG, COG = "navigation.speedOverGround", "navigation.courseOverGroundTrue"
GPS = "n2k-socketcan.3"                 # the 24xd on this boat's device map


def build():
    """A five-hour sail under way, a marker that stops after one hour, and a day sail a week
    earlier that has no business surviving."""
    tmp = tempfile.mkdtemp()
    arc = sqlite3.connect(os.path.join(tmp, "a.db"), check_same_thread=False)
    arc.executescript(archiver.SCHEMA)
    eng = os.path.join(tmp, "engine.db")
    ec = sqlite3.connect(eng)
    ec.execute("CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT, race_id TEXT, "
               "kind TEXT, start_ts REAL, end_ts REAL)")
    ec.execute("INSERT INTO sessions (name, kind, start_ts, end_ts) VALUES ('race','race',?,?)",
               (RACE_START, MARKER_END))
    ec.commit()
    rows = []
    t = RACE_START
    while t <= RACE_END:                                    # under way at 6 kn on 020 the whole way
        rows.append((iso(t), "sr33", GPS, SOG, 3.09, None))         # m/s
        rows.append((iso(t), "sr33", GPS, COG, 0.349, None))        # rad = 20 deg
        t += 60
    # a day sail a week before the race — out of session, old: this one SHOULD go
    for k in range(30):
        rows.append((iso(RACE_START - 7 * 86400 + k * 60), "sr33", GPS, SOG, 2.0, None))
    arc.executemany("INSERT INTO readings (time, boat_id, source, path, value, str_value) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows)
    arc.commit()
    return arc, eng


def in_race(arc, after):
    """Rows still present strictly after `after` and inside the sail."""
    return arc.execute("SELECT count(*) FROM readings WHERE time > ? AND time <= ?",
                       (iso(after), iso(RACE_END))).fetchone()[0]


print("session_windows:")
arc, eng = build()
markers = archiver.session_windows(eng)
derived = archiver.session_windows(eng, conn=arc)
check("the marker alone is one window; with the archive there are two",
      len(markers) == 1 and len(derived) == 2)
check("...and the extra one runs past the button, to the end of the underway record",
      derived[1][1] > markers[0][1] and derived[1][1] >= iso(RACE_END - 300))
check("the marker window is still in the set — the kept set can only grow",
      markers[0] in derived)

print("\nprune:")
before = in_race(arc, MARKER_END)
deleted = archiver.prune(arc, engine_db=eng, retain_days=14)
after = in_race(arc, MARKER_END)
check(f"the four hours after the button SURVIVE ({before} rows before, {after} after)",
      before > 200 and after == before)
check("the LAST reading of the sail survives — the derived end is a 5-minute bucket label, so "
      "an unpadded keep-window deletes the tail of every race",
      arc.execute("SELECT count(*) FROM readings WHERE time = ?",
                  (iso(RACE_END),)).fetchone()[0] == 2)
check("the old day sail is still pruned — this is a retention prune, not an amnesty",
      deleted == 30 and arc.execute(
          "SELECT count(*) FROM readings WHERE time < ?",
          (iso(RACE_START),)).fetchone()[0] == 0)
check("a second prune deletes nothing (idempotent)",
      archiver.prune(arc, engine_db=eng, retain_days=14) == 0)

print("\nthe fail-safes, which matter more than the feature:")
arc2, eng2 = build()
check("engine DB unreadable -> prune SKIPPED, as before",
      archiver.prune(arc2, engine_db=eng2 + ".nope", retain_days=14) is None)
check("retention 0 -> disabled, as before",
      archiver.prune(arc2, engine_db=eng2, retain_days=0) is None)

real_rw, archiver.race_window = archiver.race_window, None
archiver._DERIVED_CACHE.clear()
fallback = archiver.session_windows(eng2, conn=arc2)
archiver.race_window = real_rw
check("no shared/race_window in the image -> the marker windows alone (and it says so above)",
      fallback == markers)

archiver._DERIVED_CACHE.clear()
broken = sqlite3.connect(":memory:")            # no `readings` table: derivation raises
real_ms, archiver.motion_series = archiver.motion_series, \
    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
survived = archiver.session_windows(eng2, conn=arc2)
archiver.motion_series = real_ms
check("a derivation that RAISES keeps the marker window rather than deleting on a guess",
      survived == markers)
broken.close()

print("\nthe cache does not outlive the truth:")
archiver._DERIVED_CACHE.clear()
arc3, eng3 = build()
first = archiver.session_windows(eng3, conn=arc3)
arc3.executemany("INSERT INTO readings (time, boat_id, source, path, value) VALUES (?,?,?,?,?)",
                 [(iso(RACE_END + k * 60), "sr33", GPS, SOG, 3.09) for k in range(1, 61)]
                 + [(iso(RACE_END + k * 60), "sr33", GPS, COG, 0.349) for k in range(1, 61)])
arc3.commit()
grown = archiver.session_windows(eng3, conn=arc3)
check("a settled session is cached (the Pi must not re-scan the archive every hour)",
      grown == first and len(archiver._DERIVED_CACHE) == 1)
archiver._DERIVED_CACHE.clear()
check("...and clearing it picks up the hour of telemetry that arrived since",
      archiver.session_windows(eng3, conn=arc3)[1][1] > first[1][1])

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
