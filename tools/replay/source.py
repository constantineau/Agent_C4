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
_AGENT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "vps", "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

os.environ.setdefault("DATA_SOURCE", "onboard")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")   # no Signal K aboard a replay — archive only

from app.datasource_onboard import BOAT_ID, OnboardSource, _epoch, _cutoff_str  # noqa: E402


class ReplaySource(OnboardSource):
    """OnboardSource that can see no further into the race than `at`.

    `at` is epoch seconds and is moved by the harness between frames; it must be kept in step
    with the frozen process clock (`ReplayClock` in harness.py does both together)."""

    def __init__(self, at=None):
        self.at = at
        super().__init__()

    # --- the upper bound -----------------------------------------------------
    def _upper(self):
        """Second-precision UTC cutoff string that lexicographically admits all of second `at`."""
        return datetime.fromtimestamp(self.at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"

    # --- overrides: every archive read that is otherwise unbounded above ------
    def latest_value(self, path):
        """Freshest value AT OR BEFORE `at`. The live cache is off in replay, so archive only."""
        row = self._archive.execute(
            "SELECT value FROM readings WHERE boat_id=? AND path=? AND value IS NOT NULL "
            "AND time <= ? ORDER BY time DESC LIMIT 1", (BOAT_ID, path, self._upper()),
        ).fetchone()
        return row["value"] if row else None

    def series(self, path, minutes):
        def fetch():
            cut_s, cut_e = _cutoff_str(minutes)       # virtual-correct: the clock is frozen
            rows = self._archive.execute(
                "SELECT max(time) AS time, value FROM readings WHERE boat_id=? AND path=? "
                "AND value IS NOT NULL AND time > ? AND time <= ? "
                "GROUP BY substr(time,1,19) ORDER BY time",
                (BOAT_ID, path, cut_s, self._upper()),
            ).fetchall()
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
            rows = self._archive.execute(
                "SELECT source, max(time) AS time, value FROM readings WHERE boat_id=? AND path=? "
                "AND value IS NOT NULL AND time > ? AND time <= ? "
                "GROUP BY source, substr(time,1,19) ORDER BY time",
                (BOAT_ID, path, cut_s, self._upper()),
            ).fetchall()
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
        rows = self._archive.execute(
            f"SELECT path, source, max(time) AS time, value FROM readings WHERE boat_id=? "
            f"AND path IN ({placeholders}) AND value IS NOT NULL AND time > ? AND time <= ? "
            f"GROUP BY path, source", (BOAT_ID, *paths, cut_s, self._upper()),
        ).fetchall()
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
        cut_s, cut_e = _cutoff_str(max_age_min)
        rows = self._archive.execute(
            "SELECT source, max(time) AS last, count(DISTINCT path) AS paths, count(*) AS n "
            "FROM readings WHERE boat_id=? AND time > ? AND time <= ? GROUP BY source "
            "ORDER BY source", (BOAT_ID, cut_s, self._upper()),
        ).fetchall()
        out = []
        for r in rows:
            e = _epoch(r["last"])
            if e is None or e < cut_e:
                continue
            out.append({"source": r["source"], "last_epoch": e,
                        "paths": r["paths"], "samples": r["n"]})
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
