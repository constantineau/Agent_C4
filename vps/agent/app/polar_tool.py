"""Phase 6.3 — polar mining (observed vs ORC gospel).

The ORC Speed Guide (`polars` table) is the *rated* potential of the boat. This module mines
the telemetry ARCHIVE for what the boat ACTUALLY achieved, bucketed by (TWS, TWA), and compares
the two — so the crew can see where they're meeting the polar and where they're leaving speed on
the table (a sail trim / mode / sail-selection coaching signal, and a sanity check on whether the
rated polar is soft for this boat).

Method:
  * Time-bucket `telemetry_raw` (default 30 s) and pivot STW / TWS / |TWA| onto each bucket — so a
    "sample" is one short steady slice of sailing, robust to the three channels landing on slightly
    different sub-second timestamps or coming from different sources (collect-everything).
  * Bin each sample by TWS (to the ORC 2-kn grid) and TWA (default 15° bins).
  * For each (TWS, TWA) bin with enough samples, take a HIGH PERCENTILE of observed STW (default
    90th) as "best achievable" — this rejects momentary surf/GPS spikes (a max would chase noise)
    while still representing potential, not the average cruise.
  * Compare to the ORC target boatspeed at the bin centre (nearest polar bucket).

Caveats (surfaced in the output): aggregates span ALL sources for a path (a flaky paddlewheel STW
or uncalibrated wind can bias a bin); the observed polar reflects sea-state/current/crew on the
days mined, not a controlled flat-water test; >100% of polar can mean favourable current or a soft
rating, not necessarily that the boat is overperforming. This is a debrief/coaching tool, not live.
"""
import os
from datetime import datetime, timezone

from .db import pool

BOAT_ID = os.environ.get("BOAT_ID", "sr33")

_KN = 1.943844
_DEG = 57.295779513

# Tunables (first-cut; revisit against real race archives).
MINE_HOURS = float(os.environ.get("POLAR_MINE_HOURS", "168"))      # default look-back: 7 days
BUCKET_SECONDS = int(os.environ.get("POLAR_BUCKET_SECONDS", "30"))  # one "sample" = this slice
MIN_SAMPLES = int(os.environ.get("POLAR_MIN_SAMPLES", "6"))         # min slices to trust a bin
PCTILE = float(os.environ.get("POLAR_PCTILE", "90"))               # "best achievable" percentile
TWS_BIN = float(os.environ.get("POLAR_TWS_BIN", "2"))             # ORC grid step
TWA_BIN = float(os.environ.get("POLAR_TWA_BIN", "15"))           # TWA bin width (deg)
MIN_STW_KN = float(os.environ.get("POLAR_MIN_STW", "0.5"))       # drop near-stationary slices

_STW = "navigation.speedThroughWater"
_TWS = "environment.wind.speedTrue"
_TWA = "environment.wind.angleTrueWater"


def _percentile(values, pct):
    """Linear-interpolated percentile of a list (values need not be sorted)."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _point_of_sail(twa):
    if twa <= 70:
        return "upwind"
    if twa <= 120:
        return "reaching"
    return "downwind"


def _target_for(tws, twa):
    """Nearest ORC polar target_stw for a bin centre (same nearest-bucket logic as get_polar_target)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT target_stw FROM polars WHERE boat_id=%s "
            "ORDER BY (abs(tws-%s)+abs(twa-%s)) LIMIT 1",
            (BOAT_ID, tws, abs(twa)),
        ).fetchone()
    return row["target_stw"] if row else None


def mine(hours=None, min_samples=None, percentile=None):
    """Mine the archive into observed-vs-target polar bins. Returns a structured analysis dict."""
    hours = MINE_HOURS if hours is None else float(hours)
    min_samples = MIN_SAMPLES if min_samples is None else int(min_samples)
    percentile = PCTILE if percentile is None else float(percentile)
    end = datetime.now(timezone.utc)

    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT avg(value) FILTER (WHERE path=%s) AS stw, "
            "       avg(value) FILTER (WHERE path=%s) AS tws, "
            "       avg(value) FILTER (WHERE path=%s) AS twa "
            "FROM telemetry_raw "
            "WHERE boat_id=%s AND time > now() - %s::interval AND value IS NOT NULL "
            "  AND path IN (%s,%s,%s) "
            "GROUP BY time_bucket(%s, time) "
            "HAVING avg(value) FILTER (WHERE path=%s) IS NOT NULL "
            "   AND avg(value) FILTER (WHERE path=%s) IS NOT NULL "
            "   AND avg(value) FILTER (WHERE path=%s) IS NOT NULL",
            (_STW, _TWS, _TWA, BOAT_ID, f"{hours} hours", _STW, _TWS, _TWA,
             f"{BUCKET_SECONDS} seconds", _STW, _TWS, _TWA),
        ).fetchall()

    # Bin samples by (TWS grid, TWA bin); collect STW (kn) per bin.
    bins = {}
    total = 0
    for r in rows:
        stw = r["stw"] * _KN
        if stw < MIN_STW_KN:
            continue
        tws = r["tws"] * _KN
        twa = abs(r["twa"] * _DEG)
        if twa > 180:
            twa = 360 - twa
        tws_bin = round(tws / TWS_BIN) * TWS_BIN
        twa_bin = round(twa / TWA_BIN) * TWA_BIN
        bins.setdefault((tws_bin, twa_bin), []).append(stw)
        total += 1

    buckets = []
    for (tws_bin, twa_bin), stws in sorted(bins.items()):
        if len(stws) < min_samples:
            continue
        best = _percentile(stws, percentile)
        median = _percentile(stws, 50)
        target = _target_for(tws_bin, twa_bin)
        pct = round(100 * best / target) if target else None
        buckets.append({
            "tws_kn": tws_bin, "twa_deg": twa_bin, "point_of_sail": _point_of_sail(twa_bin),
            "samples": len(stws), "minutes": round(len(stws) * BUCKET_SECONDS / 60.0, 1),
            "best_stw_kn": round(best, 2), "median_stw_kn": round(median, 2),
            "target_stw_kn": round(target, 2) if target else None,
            "percent_of_polar": pct,
        })

    rated = [b for b in buckets if b["percent_of_polar"] is not None]
    weighted = sum(b["percent_of_polar"] * b["samples"] for b in rated)
    wsum = sum(b["samples"] for b in rated)
    overall_pct = round(weighted / wsum) if wsum else None

    # Roll up by point of sail (sample-weighted mean % of polar).
    pos = {}
    for b in rated:
        p = pos.setdefault(b["point_of_sail"], {"w": 0, "n": 0, "buckets": 0})
        p["w"] += b["percent_of_polar"] * b["samples"]
        p["n"] += b["samples"]
        p["buckets"] += 1
    by_pos = {k: {"percent_of_polar": round(v["w"] / v["n"]) if v["n"] else None,
                  "samples": v["n"], "buckets": v["buckets"]} for k, v in pos.items()}

    ranked = sorted(rated, key=lambda b: b["percent_of_polar"])
    weakest = ranked[:3]
    strongest = list(reversed(ranked[-3:]))

    return {
        "available": bool(buckets),
        "as_of": end.isoformat(),
        "window_hours": hours,
        "params": {"bucket_seconds": BUCKET_SECONDS, "min_samples": min_samples,
                   "percentile": percentile, "tws_bin": TWS_BIN, "twa_bin": TWA_BIN},
        "samples_total": total,
        "buckets_rated": len(rated),
        "overall_percent_of_polar": overall_pct,
        "by_point_of_sail": by_pos,
        "weakest": weakest,
        "strongest": strongest,
        "observed_polar": buckets,
        "note": ("'best_stw_kn' is the {p:.0f}th-percentile observed boatspeed in each (TWS,TWA) "
                 "bin — best achievable, not average. % of polar vs the ORC rated target. "
                 "Aggregates span all sources; sea-state/current/crew vary across the window; "
                 ">100% can be current or a soft rating. Coaching/debrief tool, not live."
                 ).format(p=percentile),
    }


def get_polar_analysis(hours: float = None, min_samples: int = None, point_of_sail: str = None):
    """Observed-vs-rated polar from the archive. Optionally filter the bin list to one point of
    sail (upwind/reaching/downwind); the summary roll-ups always cover everything mined."""
    a = mine(hours=hours, min_samples=min_samples)
    if not a.get("available"):
        a["note"] = ("Not enough archived telemetry yet to mine a polar (need STW + true wind "
                     "over time). Sail/backfill more, then retry.")
        return a
    if point_of_sail:
        pos = point_of_sail.lower()
        a["observed_polar"] = [b for b in a["observed_polar"] if b["point_of_sail"] == pos]
        a["filtered_to"] = pos
    return a
