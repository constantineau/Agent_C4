"""Race Rewind — ground truth, straight from the archive with no engine in the way.

The left pane of the review UI shows what the iPad computed; this builds what was actually
happening, so the two can be read side by side. Deliberately dumb: raw archived readings,
per source, no derivation, no engine modules. If a tile disagrees with this, the tile is wrong.

Per source matters. The boat carries several wind sources (masthead, derived, N2K) and the
engine picks among them; a tile can be wrong because the maths is wrong OR because it read a
bad source, and only a per-source view tells those apart. That is also why `environment.wind.
directionTrue` is worth watching closely here — two signalk-derived-data calcs both published
to it until 2026-09-01, so one path under one $source carried two different quantities.

Usage:
    python tools/replay/truth.py --start 2026-07-18T17:03:31Z --end 2026-07-18T20:40:00Z \
        --step 30 --out /home/constantineau/backups/replay-jul18/timeline
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:                 # so `shared.*` imports work from any cwd
    sys.path.insert(0, _ROOT)

from shared import n2k_sources            # noqa: E402

MS_TO_KN = 1.943844
DEG = 57.29577951308232

# path -> (label, unit, converter). Raw SI in the archive; converted here for reading.
PATHS = {
    "navigation.position.latitude":   ("Latitude", "deg", lambda v: v),
    "navigation.position.longitude":  ("Longitude", "deg", lambda v: v),
    "navigation.speedOverGround":     ("SOG", "kn", lambda v: v * MS_TO_KN),
    "navigation.courseOverGroundTrue": ("COG", "deg", lambda v: (v * DEG) % 360),
    "navigation.headingTrue":         ("Heading", "deg", lambda v: (v * DEG) % 360),
    "navigation.speedThroughWater":   ("STW", "kn", lambda v: v * MS_TO_KN),
    "environment.wind.speedTrue":     ("TWS", "kn", lambda v: v * MS_TO_KN),
    "environment.wind.directionTrue": ("TWD", "deg", lambda v: (v * DEG) % 360),
    "environment.wind.angleTrueWater": ("TWA", "deg", lambda v: v * DEG),
    "environment.wind.speedApparent": ("AWS", "kn", lambda v: v * MS_TO_KN),
    "environment.wind.angleApparent": ("AWA", "deg", lambda v: v * DEG),
    # Added 2026-09-08 with the full-race timeline. Attitude is the whole subject of the 22:58Z
    # kick — roll stepped to 133 deg on the 24xd while the autopilot still read 35 — and the
    # reference pane could not show it, so the one event the rig was extended to study had no
    # ground truth to judge the tile against. Signed, NOT wrapped to 0-360: an inverted sensor
    # reading -175 deg must not be displayed as a plausible 185.
    "navigation.attitude.roll":       ("Roll", "deg", lambda v: v * DEG),
    "navigation.attitude.pitch":      ("Pitch", "deg", lambda v: v * DEG),
    "navigation.rateOfTurn":          ("Rate of turn", "deg/s", lambda v: v * DEG),
    # ...and the bank, for the same reason: there is a tile for it now, and the pane it is
    # judged against should carry the number the tile claims to be reading.
    "electrical.batteries.0.voltage": ("House bank", "V", lambda v: v),
}


# A source publishing these is an AIS receiver, not an own-ship instrument. Until 2026-09-07
# the archiver had no vessel-context filter, so AIS traffic went into `readings` under the
# own-ship boat_id. Showing it here as a "source" for own-ship truth is worse than useless: it
# is what made the review pane flag disagreements between the boat and a passing ship.
AIS_MARKER_PATHS = n2k_sources.AIS_MARKER_PATHS   # canonical: shared/n2k_sources


def _ais_sources(conn, tables, probe_rows=5000):
    found = set()
    for p in AIS_MARKER_PATHS:
        for tbl in tables:
            found.update(r[0] for r in conn.execute(
                f"SELECT DISTINCT source FROM (SELECT source FROM {tbl} WHERE path=? "
                f"ORDER BY time DESC LIMIT ?)", (p, probe_rows)))
    return found


def build(archive_db, start, end, step_s, out_dir, boat_id="sr33", spool_db=None):
    """Ground truth over the archive, and over the spool when one is given.

    The spool matters here as much as it does in the frames: build a full-race timeline against
    an archive-only truth and the right-hand pane silently goes blank at 20:40:30Z — the failure
    mode this file's own `--start/--end/--step` warning exists for, arriving from the other
    direction. Rows from the two files are simply concatenated, because the downstream bucketing
    sorts per (path, source) and takes the freshest sample at or before each stamp; overlap
    between them would be harmless rather than double-counted."""
    conn = sqlite3.connect(f"file:{archive_db}?mode=ro", uri=True)
    tables = ["readings"]
    if spool_db:
        conn.execute("ATTACH DATABASE ? AS spool", (f"file:{spool_db}?mode=ro",))
        tables = ["main.readings", "spool.readings"]
    lo = start.strftime("%Y-%m-%dT%H:%M:%S")
    hi = end.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    ais = sorted(_ais_sources(conn, tables))
    if ais:
        print(f"[truth] excluding AIS-bearing source(s): {ais}")
    not_ais = f" AND source NOT IN ({','.join('?' * len(ais))})" if ais else ""

    # One row per (path, source, second) — the LAST sample in that second, never an average,
    # so wrap-around angles (359 deg -> 1 deg) cannot be smeared into a bogus midpoint.
    placeholders = ",".join("?" * len(PATHS))
    rows = []
    for tbl in tables:
        got = conn.execute(
            f"SELECT path, source, substr(time,1,19) AS sec, value FROM {tbl} "
            f"WHERE boat_id=? AND path IN ({placeholders}) AND value IS NOT NULL "
            f"AND time > ? AND time <= ?" + not_ais +
            f" GROUP BY path, source, sec ORDER BY sec", (boat_id, *PATHS, lo, hi, *ais),
        ).fetchall()
        print(f"[truth] {len(got):,} per-second samples from {tbl}")
        rows.extend(got)
    print(f"[truth] {len(rows):,} per-second samples across {len(PATHS)} paths")

    # bucket -> the freshest sample at or before each output timestamp, per (path, source)
    by_key = {}
    for path, source, sec, value in rows:
        e = datetime.fromisoformat(sec).replace(tzinfo=timezone.utc).timestamp()
        by_key.setdefault((path, source), []).append((e, value))

    stamps = []
    n = int((end - start).total_seconds() // step_s) + 1
    for i in range(n):
        stamps.append(start + timedelta(seconds=i * step_s))

    series, sources = {}, {}
    for (path, source), samples in by_key.items():
        label, unit, conv = PATHS[path]
        samples.sort()
        out, j, last = [], 0, None
        for T in stamps:
            ts = T.timestamp()
            while j < len(samples) and samples[j][0] <= ts:
                last = samples[j][1]
                j += 1
            out.append(None if last is None else round(conv(last), 4))
        series.setdefault(path, {})[source] = out
        sources.setdefault(path, []).append(source)

    doc = {
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "step_s": step_s,
        "t": [T.isoformat().replace("+00:00", "Z") for T in stamps],
        "labels": {p: {"label": v[0], "unit": v[1]} for p, v in PATHS.items()},
        "sources": {p: sorted(s) for p, s in sources.items()},
        "series": series,
        "note": "Raw archive readings per source. No engine, no derivation — the reference the "
                "iPad panes are judged against.",
    }
    os.makedirs(out_dir, exist_ok=True)
    path_out = os.path.join(out_dir, "truth.json")
    with open(path_out, "w") as fh:
        json.dump(doc, fh)
    multi = {p: s for p, s in doc["sources"].items() if len(s) > 1}
    print(f"[truth] {len(stamps)} stamps -> {path_out}")
    if multi:
        print(f"[truth] paths carrying more than one source: {json.dumps(multi, indent=2)}")
    return doc


def _iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main():
    ap = argparse.ArgumentParser(description="Build the Race Rewind ground-truth series.")
    ap.add_argument("--archive", default="/home/constantineau/backups/c4-boat-pull-2026-08-30/"
                                         "work/archive-backfill.db")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--step", type=float, default=30.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--spool", help="materialised uplink aggregates (spool_to_sqlite.py) — pass "
                                    "the same file the frames were built with, or the "
                                    "ground-truth pane goes blank where the archive stops")
    a = ap.parse_args()
    build(a.archive, _iso(a.start), _iso(a.end), a.step, a.out, spool_db=a.spool)


if __name__ == "__main__":
    main()
