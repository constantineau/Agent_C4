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
# Below this TWA the boat is not sailing — it is head to wind, motoring, or reading a wind angle
# nothing was steering to. The ORC certificate's finest angle is 35.9°, and `_performance_bins`
# in the debrief has used a 30° floor since it was written; the observed polar did not, so the
# Jul 15 2026 race published cells at 10°, 20° and 30° TWA (5.4-5.6 kn — a motor into the wind).
# Splitting the polar per race made that inner lobe impossible to miss. Refusals are COUNTED per
# race, like every other gate here.
MIN_TWA_DEG = float(os.environ.get("POLAR_MIN_TWA_DEG", "30"))
PCTILE = 80.0
MAX_POINTS = 20000


def _gate(fixes, trust):
    dz = [(a, b) for iv in (trust.get("danger") or {}).values() for a, b in (iv or ())]
    if not dz:
        return fixes, 0
    kept = [f for f in fixes if not any(a <= f["t"] <= b for a, b in dz)]
    return kept, len(fixes) - len(kept)


def _rows(raw, keyfn, min_samples, names=None):
    """Aggregate the raw per-sample lists into polar points under a coarser key.

    Every view of this polar is a p80 over the samples that belong to it, re-pooled from the raw
    lists — NOT a p80 of p80s. Filtering to one race and pooling all races are therefore both
    exact, and they are the same code, so the per-race curve and the all-race curve can never
    drift apart (a percentile does not average).
    """
    pool = {}
    for k, stws in raw.items():
        pool.setdefault(keyfn(k), []).extend(stws)
    out = []
    for k, stws in pool.items():
        if len(stws) < min_samples:
            continue
        stws = sorted(stws)
        obs = stws[min(len(stws) - 1, int(len(stws) * PCTILE / 100.0))]
        row = dict(k)
        row.update({"stw": round(obs, 2), "median_stw": round(stws[len(stws) // 2], 2),
                    "samples": len(stws)})
        if names is not None:
            row["races"] = sorted(names.get(k, ()))
        out.append(row)
    return sorted(out, key=lambda r: (r["tws"], r["twa"], r.get("config") or "",
                                      r.get("recording") or 0))


def compute(min_samples=MIN_SAMPLES):
    """Build the observed polar from every derivable race session. Returns the full artifact.

    Four views come off ONE pass of the record, because Cole asked (2026-09-15) to see each race
    instance on its own and against the others:
      - `cells`        (TWS, TWA, config)             — the pooled polar, every race together
      - `race_cells`   (TWS, TWA, config, recording)  — one race instance, by sail
      - `race_curves`  (TWS, TWA, recording)          — one race instance, sail-agnostic: the
                                                        curve to compare races with
      - `curve`        (TWS, TWA)                     — the pooled sail-agnostic curve
    A `recording` is the race instance's window start — the same key the debrief, the stored
    track and the learning archive use, so a race means the same thing on every surface.
    """
    raw, names_by_cell = {}, {}
    race_meta = []
    for s in monitor.agent_json("/racelog/sessions")["sessions"]:
        w = s.get("window") or {}
        if not w.get("hours"):
            continue
        rec = w.get("start_ts")
        if any("unverified" in p for p in (w.get("provenance") or ())):
            race_meta.append({"recording": rec, "name": s["name"], "skipped":
                              "window derived from a source that is not this boat"})
            continue
        r = monitor.agent_json(f"/racelog/track?start={w['start_ts']}&end={w['end_ts']}"
                               f"&max_points={MAX_POINTS}", timeout=180)
        trust = monitor.agent_json(f"/racelog/trust?start={w['start_ts']}&end={w['end_ts']}",
                                   timeout=180)
        fixes, refused = _gate(r["fixes"], trust)
        sail_log = r.get("sail_log") or []
        n, not_sailing = 0, 0
        for f in fixes:
            tws, twa, stw = f.get("tws"), f.get("twa"), f.get("stw")
            if tws is None or twa is None or stw is None:     # instruments or nothing
                continue
            if tws < 1.0 or stw <= 0.3:
                continue
            twa = abs(twa)
            if twa > 180.0:
                twa = 360.0 - twa
            if twa < MIN_TWA_DEG:            # head to wind / motoring — not a polar point
                not_sailing += 1
                continue
            cfg = T.config_at(sail_log, f["t"])
            if not T._config_plausible(cfg, twa):
                cfg = None                                    # a douse nobody tapped
            key = (round(tws / TWS_STEP_KN) * TWS_STEP_KN,
                   round(twa / TWA_STEP_DEG) * TWA_STEP_DEG, cfg, rec)
            raw.setdefault(key, []).append(stw)
            names_by_cell.setdefault(key[:3], set()).add(s["name"])
            n += 1
        race_meta.append({"recording": rec, "race_id": s.get("race_id"), "session_id": s.get("id"),
                          "name": s["name"], "hours": w.get("hours"),
                          "start_ts": w.get("start_ts"), "end_ts": w.get("end_ts"),
                          "fixes": len(r["fixes"]), "refused_by_trust": refused,
                          "measured_samples": n, "refused_below_min_twa": not_sailing,
                          "trust_line": (trust.get("summary") or {}).get("line"),
                          "sail_changes": len(sail_log)})

    cells = _rows(raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("config", k[2])),
                  min_samples, names={(k[0], k[1], k[2]): v for k, v in names_by_cell.items()})
    race_cells = _rows(raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("config", k[2]),
                                       ("recording", k[3])), min_samples)
    race_curves = _rows(raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("recording", k[3])),
                        min_samples)
    curve = _rows(raw, lambda k: (("tws", k[0]), ("twa", k[1])), min_samples)
    for m in race_meta:
        if m.get("skipped"):       # a skipped session contributed nothing; saying "0 cells" would
            continue               # read as a race that sailed badly rather than one not counted
        m["cells"] = sum(1 for r in race_cells if r["recording"] == m.get("recording"))
    return {"generated_at": time.time(), "wind_source": "measured",
            "grid": {"tws_step_kn": TWS_STEP_KN, "twa_step_deg": TWA_STEP_DEG,
                     "pctile": PCTILE, "min_samples": min_samples, "min_twa_deg": MIN_TWA_DEG},
            "races": race_meta, "cells": cells, "race_cells": race_cells,
            "race_curves": race_curves, "curve": curve,
            "configs": sorted({r["config"] for r in cells if r["config"]}),
            "tws_buckets": sorted({r["tws"] for r in cells})}


def build(force=False):
    """The cached artifact, recomputed on demand. A failed refresh keeps the last good build."""
    if not force:
        try:
            with open(CACHE) as fh:
                art = json.load(fh)
            # A cache written by an older build has none of the per-race views, and serving it
            # would show an empty race filter rather than an out-of-date one — the failure this
            # project keeps meeting: present, wired, silently not in force. Treat it as a miss.
            if all(k in art for k in ("race_cells", "race_curves", "curve")):
                return art
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
