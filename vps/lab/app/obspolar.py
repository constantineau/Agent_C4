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
# Seconds of the record a cell needs before it is a polar point. Cole, 2026-09-16: "can we not
# show data that is generated with less than 60 seconds of data?" — a cell built from half a
# minute is a gust or a wave train, not a speed the boat can hold, and the p80 of a handful of
# samples is the top of the noise.
#
# ⚠️ SECONDS, not samples. `/racelog/track` buckets the archive per second and then thins evenly
# to `max_points`, so one returned fix is one second only while no thinning happened — the gate
# would otherwise tighten silently on the first race long enough to trip the cap (the shape this
# project keeps meeting: a constant limit bounding a range that is not constant). Each race's
# stride is measured from its own fix timestamps and every cell's evidence is accumulated in
# seconds, so the number on screen means the same thing on a 90-minute race and a 7-hour one.
MIN_SECONDS = float(os.environ.get("POLAR_MIN_SECONDS", "60"))
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


def _stride_s(fixes):
    """Seconds of record each returned fix stands for — the MEDIAN gap between fixes.

    The median, not the mean: Jul 18 is 1 Hz for 97% of its gaps with a handful of outages up to
    77 s (the SD card, the archiver), and a mean would call that race 1.97 s/fix and halve its
    evidence. When the agent thins a long window the gaps go uniform and the median IS the stride.
    """
    ts = sorted(f["t"] for f in fixes if f.get("t") is not None)
    if len(ts) < 2:
        return 1.0
    d = sorted(b - a for a, b in zip(ts, ts[1:]))
    return max(1.0, float(d[len(d) // 2]))


def _rows(raw, keyfn, min_seconds, strides, names=None):
    """Aggregate the raw per-sample lists into polar points under a coarser key.

    Every view of this polar is a p80 over the samples that belong to it, re-pooled from the raw
    lists — NOT a p80 of p80s. Filtering to one race and pooling all races are therefore both
    exact, and they are the same code, so the per-race curve and the all-race curve can never
    drift apart (a percentile does not average).

    Returns `(rows, refused)` — the cells that cleared `min_seconds`, and the ones that did not,
    each with the evidence it had. Every gate in this module says what it refused.
    """
    pool, secs = {}, {}
    for k, stws in raw.items():
        kk = keyfn(k)
        pool.setdefault(kk, []).extend(stws)
        secs[kk] = secs.get(kk, 0.0) + len(stws) * strides.get(k[3], 1.0)
    out, refused = [], []
    for k, stws in pool.items():
        if secs[k] < min_seconds:
            refused.append(dict(k, samples=len(stws), seconds=round(secs[k])))
            continue
        stws = sorted(stws)
        obs = stws[min(len(stws) - 1, int(len(stws) * PCTILE / 100.0))]
        row = dict(k)
        row.update({"stw": round(obs, 2), "median_stw": round(stws[len(stws) // 2], 2),
                    "samples": len(stws), "seconds": round(secs[k])})
        if names is not None:
            row["races"] = sorted(names.get(k, ()))
        out.append(row)
    key = lambda r: (r["tws"], r["twa"], r.get("config") or "", r.get("recording") or 0)
    return sorted(out, key=key), sorted(refused, key=key)


def compute(min_seconds=MIN_SECONDS):
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
    raw, names_by_cell, strides = {}, {}, {}
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
        strides[rec] = _stride_s(r["fixes"])
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
                          "sec_per_fix": round(strides[rec], 2),
                          "trust_line": (trust.get("summary") or {}).get("line"),
                          "sail_changes": len(sail_log)})

    cells, thin_cells = _rows(
        raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("config", k[2])), min_seconds, strides,
        names={(k[0], k[1], k[2]): v for k, v in names_by_cell.items()})
    race_cells, thin_race_cells = _rows(
        raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("config", k[2]), ("recording", k[3])),
        min_seconds, strides)
    race_curves, thin_race_curves = _rows(
        raw, lambda k: (("tws", k[0]), ("twa", k[1]), ("recording", k[3])), min_seconds, strides)
    curve, thin_curve = _rows(raw, lambda k: (("tws", k[0]), ("twa", k[1])), min_seconds, strides)
    for m in race_meta:
        if m.get("skipped"):       # a skipped session contributed nothing; saying "0 cells" would
            continue               # read as a race that sailed badly rather than one not counted
        m["cells"] = sum(1 for r in race_cells if r["recording"] == m.get("recording"))
        # what THIS race measured but could not stand behind — the count is per race because
        # that is where it is actionable ("we only reached here for a few seconds")
        m["thin_cells"] = sum(1 for r in thin_race_cells if r["recording"] == m.get("recording"))
        m["thin_seconds"] = round(sum(r["seconds"] for r in thin_race_cells
                                      if r["recording"] == m.get("recording")))
    return {"generated_at": time.time(), "wind_source": "measured",
            "grid": {"tws_step_kn": TWS_STEP_KN, "twa_step_deg": TWA_STEP_DEG,
                     "pctile": PCTILE, "min_seconds": min_seconds, "min_twa_deg": MIN_TWA_DEG},
            "races": race_meta, "cells": cells, "race_cells": race_cells,
            "race_curves": race_curves, "curve": curve,
            "thin": {"cells": len(thin_cells), "race_cells": len(thin_race_cells),
                     "race_curves": len(thin_race_curves), "curve": len(thin_curve)},
            # the refused cells themselves, so the page can say what it is not showing at the
            # TWS/race the user is actually looking at rather than only a grand total
            "refused_thin": thin_cells, "refused_thin_race": thin_race_cells,
            "configs": sorted({r["config"] for r in cells if r["config"]}),
            "tws_buckets": sorted({r["tws"] for r in cells})}


def build(force=False):
    """The cached artifact, recomputed on demand. A failed refresh keeps the last good build."""
    if not force:
        try:
            with open(CACHE) as fh:
                art = json.load(fh)
            # A cache written by an older build has none of the per-race views, or was built
            # under the old sample-count gate, and serving it would show numbers the page says
            # are seconds — the failure this project keeps meeting: present, wired, silently not
            # in force. Treat it as a miss.
            if all(k in art for k in ("race_cells", "race_curves", "curve", "refused_thin")) \
                    and "min_seconds" in (art.get("grid") or {}):
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
