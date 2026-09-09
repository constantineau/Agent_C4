"""The OBSERVED polar — the Lab's canonical build of it (the Polar tab + the shore CLI).

Cole, 2026-09-09: "just compute the polars off of the instruments — not off of
theoretical/forecast data." One implementation, used by the API and the CLI both, because this
repo has already paid for running analyses from two copies of a thing.

What it builds: observed STW per measured (TWS, TWA[, config]) cell on a plain regular grid —
NOT a %-of-certificate report. The pipeline is the production one end to end: sessions → derived
race windows → `/racelog/track` (fixes carrying measured TWS/TWA/STW, ranked per second) +
`/racelog/trust` → the trust gate (danger windows refused) → the kite gate (impossible configs
unattributed) → p80 best-achievable per cell. Bench-derived sessions are skipped and said.

The build talks to the agent and walks two races of fixes (~30 s), so it caches to the
`lab_learning` volume; `force=True` recomputes. The cache is the artifact the tab renders —
refresh is a button, not a page load.
"""
import json
import os
import time

from . import monitor
from . import track as T

CACHE = os.path.join(os.path.dirname(os.environ.get("LEARNING_DB", "/srv/learning/learning.db")),
                     "observed_polar.json")
TWS_STEP_KN = 2.0
TWA_STEP_DEG = 10.0
MIN_SAMPLES = 30          # seconds of evidence a cell needs before it is a polar point
PCTILE = 80.0
MAX_POINTS = 20000


def _gate(fixes, trust):
    dz = [(a, b) for iv in (trust.get("danger") or {}).values() for a, b in (iv or ())]
    if not dz:
        return fixes, 0
    kept = [f for f in fixes if not any(a <= f["t"] <= b for a, b in dz)]
    return kept, len(fixes) - len(kept)


def compute(min_samples=MIN_SAMPLES):
    """Build the observed polar from every derivable race session. Returns the full artifact."""
    cells, races_by_cell = {}, {}
    race_meta = []
    for s in monitor.agent_json("/racelog/sessions")["sessions"]:
        w = s.get("window") or {}
        if not w.get("hours"):
            continue
        if any("unverified" in p for p in (w.get("provenance") or ())):
            race_meta.append({"name": s["name"], "skipped":
                              "window derived from a source that is not this boat"})
            continue
        r = monitor.agent_json(f"/racelog/track?start={w['start_ts']}&end={w['end_ts']}"
                               f"&max_points={MAX_POINTS}", timeout=180)
        trust = monitor.agent_json(f"/racelog/trust?start={w['start_ts']}&end={w['end_ts']}",
                                   timeout=180)
        fixes, refused = _gate(r["fixes"], trust)
        sail_log = r.get("sail_log") or []
        n = 0
        for f in fixes:
            tws, twa, stw = f.get("tws"), f.get("twa"), f.get("stw")
            if tws is None or twa is None or stw is None:     # instruments or nothing
                continue
            if tws < 1.0 or stw <= 0.3:
                continue
            twa = abs(twa)
            if twa > 180.0:
                twa = 360.0 - twa
            cfg = T.config_at(sail_log, f["t"])
            if not T._config_plausible(cfg, twa):
                cfg = None                                    # a douse nobody tapped
            key = (round(tws / TWS_STEP_KN) * TWS_STEP_KN,
                   round(twa / TWA_STEP_DEG) * TWA_STEP_DEG, cfg)
            cells.setdefault(key, []).append(stw)
            races_by_cell.setdefault(key, set()).add(s["name"])
            n += 1
        race_meta.append({"name": s["name"], "hours": w.get("hours"),
                          "fixes": len(r["fixes"]), "refused_by_trust": refused,
                          "measured_samples": n,
                          "trust_line": (trust.get("summary") or {}).get("line"),
                          "sail_changes": len(sail_log)})

    rows = []
    for (tws, twa, cfg), stws in cells.items():
        if len(stws) < min_samples:
            continue
        stws.sort()
        obs = stws[min(len(stws) - 1, int(len(stws) * PCTILE / 100.0))]
        rows.append({"tws": tws, "twa": twa, "config": cfg,
                     "stw": round(obs, 2), "median_stw": round(stws[len(stws) // 2], 2),
                     "samples": len(stws), "races": sorted(races_by_cell[(tws, twa, cfg)])})
    rows.sort(key=lambda r: (r["tws"], r["twa"], r["config"] or ""))
    return {"generated_at": time.time(), "wind_source": "measured",
            "grid": {"tws_step_kn": TWS_STEP_KN, "twa_step_deg": TWA_STEP_DEG,
                     "pctile": PCTILE, "min_samples": min_samples},
            "races": race_meta, "cells": rows,
            "configs": sorted({r["config"] for r in rows if r["config"]}),
            "tws_buckets": sorted({r["tws"] for r in rows})}


def build(force=False):
    """The cached artifact, recomputed on demand. A failed refresh keeps the last good build."""
    if not force:
        try:
            with open(CACHE) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    try:
        art = compute()
    except Exception as exc:
        try:                                     # stale beats broken, but say so
            with open(CACHE) as fh:
                stale = json.load(fh)
            stale["refresh_error"] = f"{type(exc).__name__}: {exc}"
            return stale
        except (OSError, ValueError):
            return {"cells": [], "races": [], "configs": [], "tws_buckets": [],
                    "error": f"agent unreachable and no cached build: {exc}"}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(art, fh)
    os.replace(tmp, CACHE)
    return art
