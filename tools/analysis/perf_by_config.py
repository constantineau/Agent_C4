#!/usr/bin/env python3
"""Boat speed vs polar, per SAIL CONFIGURATION, straight from the full-res record.

The Lab debrief infers helm quality from the shape of a position track. The boat records
STW, true wind and the crew's sail-bar history at 5-28 Hz, so for a race that is in Postgres the
same question can be *measured*: for every (sail config, TWS, TWA) bin, what fraction of the ORC
polar did we actually sail, and on how many seconds of evidence?

Two things this deliberately does NOT do, because both have already bitten this project:

  * it does not merge sources. TWS/TWA/STW each come from ONE ranked publisher; a series that
    alternates between two instruments is a series of manufactured changes.
  * it does not treat every sail-log row as a sail change. The log is a record of TAPS: entries
    arrive 2-3x over (the backfill cursor lives in the archive DB and backfills have run from two
    copies), and one manoeuvre logs as two or three states a second apart. Rows are deduped on
    (time, state) and then settled — a burst inside `--settle` seconds counts as the state it
    ended on.

    python3 tools/analysis/perf_by_config.py --start 2026-07-15T22:39:00Z --end 2026-07-16T00:08:00Z
"""
import argparse
import json
import os
import re
import subprocess
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
POLARS = os.path.join(ROOT, "vps", "db", "seed", "polars_sr33.sql")
PSQL = ["docker", "exec", "sr33-dev-timescaledb-1", "psql", "-U", "sr33", "-d", "sr33_dev", "-At", "-F", "\t"]
MS_TO_KN = 1.943844


def q(sql):
    out = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, check=True).stdout
    return [l.split("\t") for l in out.splitlines() if l]


def load_polars():
    rows = []
    for m in re.finditer(r"\('sr33',\s*([\d.]+),\s*([\d.]+),\s*([\d.]+),\s*([\d.-]+)\)",
                         open(POLARS).read()):
        tws, twa, stw, _vmg = (float(g) for g in m.groups())
        rows.append((tws, twa, stw))
    return rows


def target(polars, tws, twa):
    return min(polars, key=lambda p: abs(p[0] - tws) + abs(p[1] - abs(twa)))[2]


def series(path, source, t0, t1):
    """{epoch_second: median value} from ONE publisher, decimated to 1 Hz."""
    rows = q(f"""SELECT extract(epoch FROM date_trunc('second', time))::bigint, avg(value)
                 FROM telemetry_raw WHERE path = '{path}' AND source = '{source}'
                 AND time >= '{t0}' AND time <= '{t1}' AND value IS NOT NULL GROUP BY 1""")
    return {int(a): float(b) for a, b in rows}


def best_source(path, t0, t1, exclude=("derived-data", "course-provider")):
    rows = q(f"""SELECT source, count(*) FROM telemetry_raw WHERE path = '{path}'
                 AND time >= '{t0}' AND time <= '{t1}' GROUP BY 1 ORDER BY 2 DESC""")
    for s, _n in rows:
        if s not in exclude and not s.startswith("n2k-sample-data"):   # the bench leaks in here
            return s
    return rows[0][0] if rows else None


def sail_timeline(t0, t1, settle_s):
    raw = q(f"""SELECT DISTINCT extract(epoch FROM time)::bigint, str_value FROM telemetry_raw
                WHERE path = 'crew.sail.state' AND time >= '{t0}' AND time <= '{t1}' ORDER BY 1""")
    entries = []
    for ts, blob in sorted((int(a), b) for a, b in raw):
        flying = ",".join(json.loads(blob).get("flying") or []) or "(bare)"
        if entries and ts - entries[-1][0] <= settle_s:
            entries[-1] = (ts, flying)          # a burst counts as what it ended on
        else:
            entries.append((ts, flying))
    return [e for i, e in enumerate(entries) if i == 0 or e[1] != entries[i - 1][1]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--settle", type=float, default=30.0)
    ap.add_argument("--min-samples", type=int, default=60, help="seconds of evidence per bin")
    a = ap.parse_args()

    polars = load_polars()
    src = {p: best_source(p, a.start, a.end) for p in
           ("environment.wind.speedTrue", "environment.wind.angleTrueWater",
            "navigation.speedThroughWater")}
    print("sources:", json.dumps(src))
    tws = series("environment.wind.speedTrue", src["environment.wind.speedTrue"], a.start, a.end)
    twa = series("environment.wind.angleTrueWater", src["environment.wind.angleTrueWater"], a.start, a.end)
    stw = series("navigation.speedThroughWater", src["navigation.speedThroughWater"], a.start, a.end)
    sails = sail_timeline(a.start, a.end, a.settle)
    print(f"{len(tws)} s of wind, {len(stw)} s of STW, {len(sails)} settled sail configurations")
    for ts, cfg in sails:
        print(f"   {ts}  {cfg}")

    def config_at(t):
        cur = None
        for ts, cfg in sails:
            if ts <= t:
                cur = cfg
            else:
                break
        return cur

    bins = defaultdict(list)
    for t in sorted(set(tws) & set(twa) & set(stw)):
        w, angle, speed = tws[t] * MS_TO_KN, abs(twa[t]) * 57.29577951308232, stw[t] * MS_TO_KN
        if angle > 180:
            angle = 360 - angle
        if w < 1 or speed <= 0:
            continue
        bins[(config_at(t), round(w / 2) * 2, round(angle / 15) * 15)].append((speed, w, angle))

    print(f"\n{'config':16} {'TWS':>5} {'TWA':>5} {'n(s)':>6} {'STW':>6} {'target':>7} {'% polar':>8}")
    rows = []
    for (cfg, wb, ab), vals in sorted(bins.items(), key=lambda kv: -len(kv[1])):
        if len(vals) < a.min_samples or cfg is None:
            continue
        speeds = sorted(v[0] for v in vals)
        med = speeds[len(speeds) // 2]
        tgt = target(polars, sum(v[1] for v in vals) / len(vals), sum(v[2] for v in vals) / len(vals))
        pct = 100 * med / tgt if tgt else 0
        rows.append((cfg, wb, ab, len(vals), med, tgt, pct))
        print(f"{cfg:16} {wb:>5} {ab:>5} {len(vals):>6} {med:>6.2f} {tgt:>7.2f} {pct:>7.1f}%")
    if rows:
        tot = sum(r[3] for r in rows)
        print(f"\n{len(rows)} bins with >= {a.min_samples}s of evidence, {tot} s total")
        for cfg in sorted({r[0] for r in rows}):
            rs = [r for r in rows if r[0] == cfg]
            n = sum(r[3] for r in rs)
            print(f"   {cfg:16} {n:>6} s  weighted {sum(r[6] * r[3] for r in rs) / n:>5.1f}% of polar")

    # PAIRED comparison. Config totals are not comparable on their own — each config was flown in
    # its own slice of wind, so "A3 90.5% vs A3+SS 86.1%" compares two different races. Within one
    # (TWS, TWA) cell the comparison is real, and it is the only way to answer questions the ORC
    # certificate cannot: it rates single sails, so a combination the crew invents (A3 + staysail)
    # has no rated target anywhere — the boat's own record is the only source of truth for it.
    print(f"\npaired within a cell — the only comparison that controls for conditions:")
    by_cell = defaultdict(list)
    for cfg, wb, ab, n, med, tgt, pct in rows:
        by_cell[(wb, ab)].append((cfg, n, med, pct))
    found = False
    for (wb, ab), cfgs in sorted(by_cell.items()):
        if len(cfgs) < 2:
            continue
        found = True
        base = max(cfgs, key=lambda c: c[1])
        print(f"   TWS {wb:>2} TWA {ab:>3}:")
        for cfg, n, med, pct in sorted(cfgs, key=lambda c: -c[2]):
            d = med - base[2]
            mark = "  (reference)" if cfg == base[0] else f"  {d:+.2f} kn vs {base[0]}"
            print(f"      {cfg:14} {med:>5.2f} kn  {pct:>5.1f}%  {n:>4} s{mark}")
    if not found:
        print("   (no cell carries two configurations with enough evidence)")


if __name__ == "__main__":
    main()
