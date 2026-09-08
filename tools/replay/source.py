"""ReplaySource — OnboardSource pinned to a moment in a recorded race.

The point of the replay rig is that it runs the SAME engine modules the boat ran, so a
finding here is a finding about the real system. That means changing as little as possible:

  - **No boat code is modified.** `datasource.active()` memoises into a module global, so the
    harness assigns `datasource._SOURCE = ReplaySource(...)` before anything calls it.
  - **The process clock is frozen by the harness** (freezegun), which covers the 32 wall-clock
    sites across the 15 engine modules that read `datetime.now()` / `time.time()` directly —
    including `datasource_onboard._cutoff_str()`, so window LOWER bounds come out virtual-correct
    for free.
  - **This class only adds the UPPER bound.** Freezing the clock does not stop
    `latest_value()`'s `ORDER BY time DESC LIMIT 1` from returning a reading from later in the
    race, which would leak the future into a frame. Five archive queries need `time <= T`.

Archive `time` is a zero-padded ISO8601 string with a fractional part and a trailing `Z`, so
bounds are lexicographic. A fraction-less cutoff + `Z` sorts above every row inside that same
second ('.'(0x2E) < 'Z'(0x5A)) and below the next second, so `time <= _upper()` admits the whole
of second T — the same slightly-inclusive convention `_cutoff_str()` already uses for the lower
bound. Callers that care re-filter by parsed epoch.

Not replayable from the archive, and deliberately not faked here:
  - **AIS / fleet.** The archiver stores own-ship contexts only; AIS went to the cloud
    `ais_targets` table via a separate uplink path. `ais_targets()` returns [] and the Fleet
    tile will read empty. Postgres has the real data — wiring it in is a follow-up.
  - **Tier-2 copilot narration.** Never archived for Jul 18 (the only `crew` paths that race
    are `crew.sail.state` and `crew.session`), so the coach lines are unrecoverable.
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Import the agent app package the same way the onboard engine does.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AGENT = os.path.join(_ROOT, "vps", "agent")
for _p in (_AGENT, _ROOT):          # _ROOT so `shared.*` imports work from any cwd
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DATA_SOURCE", "onboard")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")   # no Signal K aboard a replay — archive only

from app.datasource_onboard import (AIS_FILTER, AIS_MARKER_PATHS, AIS_PROBE_ROWS,  # noqa: E402
                                    BOAT_ID, OnboardSource, _epoch, _cutoff_str)
from shared import source_policy  # noqa: E402

_AIS_CACHE = {}         # (archive, spool) -> set(source labels carrying AIS traffic)


class ReplaySource(OnboardSource):
    """OnboardSource that can see no further into the race than `at`.

    `at` is epoch seconds and is moved by the harness between frames; it must be kept in step
    with the frozen process clock (`ReplayClock` in harness.py does both together)."""

    def __init__(self, at=None, spool_db=None):
        self.at = at
        # A second, coarser recording of the SAME race — see spool_to_sqlite.py. The Pi's
        # archive stops at 20:40:30Z where the SD card corrupted; the uplink aggregates carry
        # the rest, including the retirement and the kicked GPS at 22:58Z. Attached rather than
        # merged so the two sample rates stay distinguishable; every window read goes through
        # `_both()`, so not one engine module has to know there are two files.
        self.spool_db = spool_db or os.environ.get("REPLAY_SPOOL_DB") or None
        super().__init__()

    # --- the upper bound -----------------------------------------------------
    def _upper(self):
        """Second-precision UTC cutoff string that lexicographically admits all of second `at`."""
        return datetime.fromtimestamp(self.at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    # --- two files, read as one ----------------------------------------------
    @property
    def _archive(self):
        """The parent's per-thread read-only connection, with the spool ATTACHed.

        Attached per thread on the same first-use path the parent uses, so a threadpool worker
        that has not been used yet still gets it. Read-only URI on both, so neither file is ever
        written — no `-wal` appears next to a 3.1 GB archive we depend on."""
        fresh = getattr(self._archive_local, "conn", None) is None
        conn = OnboardSource._archive.fget(self)
        if fresh and self.spool_db:
            conn.execute("ATTACH DATABASE ? AS spool", (f"file:{self.spool_db}?mode=ro",))
        return conn

    def _both(self, inner, params, outer, group_by=None, order_by=None):
        """Run an aggregate over archive + spool, aggregating INSIDE each arm.

        `inner` is a per-file SELECT with a `{tbl}` slot; `outer` re-aggregates the two arms'
        (tiny) results. With no spool attached there is one arm and the results are the same as
        the single-file query this replaced.

        The obvious implementation — one `CREATE TEMP VIEW readings_all AS ... UNION ALL ...` and
        no other changes — was tried first and is 200x slower. Its plan looks right (both arms
        SEARCH their `readings_path_time_idx`) but the compound sits behind a CO-ROUTINE that the
        outer `max()` then SCANS, so every read streams every matching row of a 10.4 M-row table
        instead of taking the last index entry: 0.67 s versus 0.00 s per read, which turned a
        13-frame build into something that had not finished in ten minutes. Aggregating inside
        each arm keeps `max(time)` on the index at both ends.

        `value` rides along as a bare column beside `max(time)` — SQLite's documented min/max
        rule picks the bare columns from the row holding the extreme, and that holds in the outer
        query too, so the later of the two arms wins. The existing single-file queries already
        depend on that rule; this preserves it rather than inventing a tie-break.

        The outer aggregate is applied even with ONE arm, so callers see a single result shape
        either way. A first version skipped the wrapper without a spool and made every caller
        branch on `self.spool_db` to know what it was holding — the sort of two-shapes-one-name
        seam that this repo keeps finding at the bottom of its bugs. One row per group either
        way costs nothing."""
        arms = ["main.readings", "spool.readings"] if self.spool_db else ["readings"]
        sql = (f"SELECT {outer} FROM ("
               + " UNION ALL ".join(inner.format(tbl=a) for a in arms) + ")")
        if group_by:
            sql += f" GROUP BY {group_by}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        return self._archive.execute(sql, tuple(params) * len(arms)).fetchall()

    def ais_sources(self):
        """Discover AIS-bearing sources across BOTH files, not just the archive.

        The parent probes `readings` in the main database. That happens to be enough for Jul 18
        — `n2k-socketcan.43` publishes AIS markers in both files — but "happens to be enough" is
        how the AIS contamination bug survived a race. A spool window whose AIS transceiver never
        appears in the archive would otherwise have its foreign positions read as own-ship, and
        the failure mode is a plausible-looking number, not an error."""
        if not AIS_FILTER:
            return set()
        key = (os.environ.get("ARCHIVE_DB"), self.spool_db)
        cached = _AIS_CACHE.get(key)
        if cached is not None:
            return cached
        found = set()
        for p in AIS_MARKER_PATHS:
            try:
                found.update(r[0] for r in self._both(
                    "SELECT DISTINCT source FROM (SELECT source FROM {tbl} "
                    "WHERE path=? ORDER BY time DESC LIMIT ?)",
                    (p, AIS_PROBE_ROWS), "DISTINCT source"))
            except Exception:
                continue
        _AIS_CACHE[key] = found
        print(f"[replay] AIS-bearing source(s) excluded from own-ship reads: {sorted(found)}"
              if found else "[replay] no AIS-bearing sources in archive+spool", flush=True)
        return found

    # --- overrides: every archive read that is otherwise unbounded above ------
    def latest_value(self, path):
        """Priority-preferred value AT OR BEFORE `at`; archive only (the live cache is off).

        Mirrors `OnboardSource.latest_value` with the upper bound added — same two bounded
        queries, same `_prefer`. It has to: the rig exists to measure what the boat does, so a
        read-path change that skipped this override would be measured as if it had never
        shipped."""
        not_ais, ais_p = self._not_ais()
        newest = (self._both(
            "SELECT max(time) AS time FROM {tbl} WHERE boat_id=? AND path=? "
            "AND value IS NOT NULL AND time <= ?" + not_ais,
            (BOAT_ID, path, self._upper(), *ais_p), "max(time) AS time") or [None])[0]
        if not newest or not newest["time"]:
            return None
        newest_e = _epoch(newest["time"])
        if newest_e is None:
            return None
        cut = datetime.fromtimestamp(
            newest_e - source_policy.FAILOVER_AGE_S, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        rows = self._both(
            "SELECT source, max(time) AS time, value FROM {tbl} WHERE boat_id=? AND path=? "
            "AND value IS NOT NULL AND time > ? AND time <= ?" + not_ais + " GROUP BY source",
            (BOAT_ID, path, cut, self._upper(), *ais_p),
            "source, max(time) AS time, value", group_by="source")
        cands = [(r["source"], _epoch(r["time"]), r["value"]) for r in rows]
        best = self._prefer(path, [c for c in cands if c[1] is not None])
        return best[2] if best else None

    def series(self, path, minutes):
        def fetch():
            cut_s, cut_e = _cutoff_str(minutes)       # virtual-correct: the clock is frozen
            not_ais, ais_p = self._not_ais()
            rows = self._both(
                "SELECT max(time) AS time, value FROM {tbl} WHERE boat_id=? AND path=? "
                "AND value IS NOT NULL AND time > ? AND time <= ?" + not_ais
                + " GROUP BY substr(time,1,19)",
                (BOAT_ID, path, cut_s, self._upper(), *ais_p),
                "max(time) AS time, value", group_by="substr(time,1,19)", order_by="time")
            out = []
            for r in rows:
                e = _epoch(r["time"])
                if e is not None and e >= cut_e:
                    out.append((e, float(r["value"])))
            return out
        return self._cached_series("series", path, minutes, fetch)

    def series_by_source(self, path, minutes):
        def fetch():
            cut_s, cut_e = _cutoff_str(minutes)
            not_ais, ais_p = self._not_ais()
            rows = self._both(
                "SELECT source, max(time) AS time, value FROM {tbl} WHERE boat_id=? AND path=? "
                "AND value IS NOT NULL AND time > ? AND time <= ?" + not_ais
                + " GROUP BY source, substr(time,1,19)",
                (BOAT_ID, path, cut_s, self._upper(), *ais_p),
                "source, max(time) AS time, value",
                group_by="source, substr(time,1,19)", order_by="time")
            out = []
            for r in rows:
                e = _epoch(r["time"])
                if e is not None and e >= cut_e:
                    out.append((r["source"], e, float(r["value"])))
            return out
        return self._cached_series("by_source", path, minutes, fetch)

    def latest_per_source(self, paths, max_age_min):
        cut_s, cut_e = _cutoff_str(max_age_min)
        if not paths:
            return []
        placeholders = ",".join("?" * len(paths))
        not_ais, ais_p = self._not_ais()
        rows = self._both(
            "SELECT path, source, max(time) AS time, value FROM {tbl} WHERE boat_id=? "
            f"AND path IN ({placeholders}) AND value IS NOT NULL AND time > ? AND time <= ?"
            + not_ais + " GROUP BY path, source",
            (BOAT_ID, *paths, cut_s, self._upper(), *ais_p),
            "path, source, max(time) AS time, value", group_by="path, source")
        best = {}
        for r in rows:
            e = _epoch(r["time"])
            if e is None or e < cut_e:
                continue
            k = (r["path"], r["source"])
            if k not in best or e > best[k][0]:
                best[k] = (e, float(r["value"]))
        return [{"path": p, "source": s, "value": v, "epoch": e}
                for (p, s), (e, v) in best.items()]

    def sources(self, max_age_min):
        """Two queries rather than one, because `count(DISTINCT path)` does not add up.

        Summing each arm's distinct-path count double-counts every path a source publishes on
        both sides of the seam — which, across the 20:40:30Z boundary, is most of them. So the
        counts that DO add (samples) and the extreme that does (last seen) come from one query,
        and the distinct paths from a second over the (source, path) pairs, which is a few
        hundred rows at most."""
        cut_s, cut_e = _cutoff_str(max_age_min)
        not_ais, ais_p = self._not_ais()
        params = (BOAT_ID, cut_s, self._upper(), *ais_p)
        rows = self._both(
            "SELECT source, max(time) AS last, count(*) AS n "
            "FROM {tbl} WHERE boat_id=? AND time > ? AND time <= ?" + not_ais
            + " GROUP BY source",
            params, "source, max(last) AS last, sum(n) AS n",
            group_by="source", order_by="source")
        pathcount = {r["source"]: r["paths"] for r in self._both(
            "SELECT DISTINCT source, path FROM {tbl} "
            "WHERE boat_id=? AND time > ? AND time <= ?" + not_ais,
            params, "source, count(DISTINCT path) AS paths", group_by="source")}
        out = []
        for r in rows:
            e = _epoch(r["last"])
            if e is None or e < cut_e:
                continue
            out.append({"source": r["source"], "last_epoch": e,
                        "paths": pathcount.get(r["source"], 0), "samples": r["n"]})
        return out

    # --- caches must not survive a clock move --------------------------------
    def _cached_series(self, kind, path, minutes, fetch):
        """The parent memoises window reads for 3 s of WALL time so the dashboard's nine
        parallel polls share one query. Under replay the clock jumps between frames while
        little wall time passes, so that cache would serve one frame's data to the next.
        Key it on `at` instead — still collapses the parallel reads within a frame."""
        key = (kind, path, round(float(minutes), 2), self.at)
        with self._series_lock:
            hit = self._series_cache.get(key)
            if hit is not None:
                return hit[1]
        rows = fetch()
        with self._series_lock:
            self._series_cache[key] = (self.at, rows)
            if len(self._series_cache) > 512:
                # drop everything from older frames — we only ever revisit the current `at`
                for k in [k for k in self._series_cache if k[3] != self.at]:
                    del self._series_cache[k]
        return rows

    def ais_targets(self, max_age_min):
        """No AIS in the archive (own-ship contexts only) — see the module docstring."""
        return []
