"""Lab-4 learning loop — the ONGOING performance archive + human-approved boat-model refinement.

Every debrief run is archived to a persistent SQLite DB (the `lab_learning` volume) so race
performance is kept for future review — the regret + helm-vs-optimal metrics and the observed-vs-polar
performance bins, plus the full report JSON. From that accumulating record the loop PROPOSES refinements
to the boat model — a refined `helm_factor` (overall achievable fraction) and per-(TWS,TWA)-cell polar
overlay multipliers (the boat's speed SHAPE vs the ORC cert) — but it never applies them. A human
REVIEWS and APPROVES (or edits / rejects) every change before it lands on the boat profile; the ORC
cert stays the canonical source, and approved tweaks are an explicit, reviewable overlay
(`BoatProfile.polar_adjustments`, applied at optimize time via `polars.apply_adjustments`). This is the
project's database-safety + human-review ethos: nothing mutates the boat's polars without a person
signing off.

Pure-stdlib `sqlite3` (no new deps), mirroring the onboard engine's SQLite stores.
"""
import json
import os
import sqlite3
import time

LEARNING_DB = os.environ.get("LEARNING_DB", "/srv/learning/learning.db")

# proposal guardrails — keep an approved auto-suggestion from ever wildly distorting the polar. The
# helm factor MAY exceed 1.0: the ORC polar is a (conservative) rating, not a physical ceiling — a
# soft-rated / well-sailed boat genuinely sails above the cert, and the current-corrected measurement
# is what makes a >100% trustworthy. The cap is symmetric so we can learn "faster than rated" too.
_HELM_MIN, _HELM_MAX = 0.50, 1.15
_CELL_MULT_MIN, _CELL_MULT_MAX = 0.85, 1.15
_CELL_DEADBAND = 0.04            # don't propose a cell tweak smaller than ±4% (noise)
_MIN_RACES_FOR_CELL = 1          # a cell must appear in at least this many races to be proposed

# --- wave-coefficient calibration guardrails (Lab-4 condition attribution) ---------------------
# The env priors are deliberately conservative; a fit from real logs can only move k within a sane
# physical band, needs enough sea-state SPREAD to be meaningful, and is human-approved before it lands.
_WAVE_K_MIN, _WAVE_K_MAX = 0.0, 0.12      # frac speed lost per m Hs above the deadband (per point of sail)
_WAVE_MIN_CELLS = 4                        # need at least this many distinct-Hs cells per point of sail
_WAVE_MIN_HS_SPREAD = 0.6                  # …spanning at least this much effective Hs (m) to fit a slope
_WAVE_POS_KEY = {"upwind": "k_up", "reaching": "k_reach", "downwind": "k_down"}
# Deadband (knee) fit: the Hs below which the boat keeps ~full speed. Only identifiable when the archive
# has a clear FLAT region (points below the knee) AND a sloped region (points above) — otherwise the
# prior deadband is held (the common sparse-archive case). The floor is NOT fit: hitting it needs extreme
# seas an archive almost never holds, so it stays the conservative prior.
_WAVE_DB_GRID = [round(0.2 + 0.1 * i, 1) for i in range(9)]   # candidate knees 0.2 … 1.0 m
_WAVE_DB_MIN_LOW = 2                        # need ≥ this many flat-region cells to locate the knee…
_WAVE_DB_MIN_HIGH = 3                       # …and ≥ this many sloped-region cells


def _conn():
    os.makedirs(os.path.dirname(LEARNING_DB), exist_ok=True)
    c = sqlite3.connect(LEARNING_DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS debriefs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL, race_id TEXT, race_name TEXT, playbook_id TEXT, boat_id TEXT,
        oracle_hours REAL, regret_min REAL, side_paid TEXT, recommended_side TEXT, side_matched INTEGER,
        track_source TEXT, elapsed_hours REAL, time_behind_min REAL, oversail_pct REAL,
        xte_mean REAL, xte_p90 REAL, xte_max REAL, side_worked TEXT,
        polar_pct REAL, polar_samples INTEGER, report_json TEXT);
    CREATE TABLE IF NOT EXISTS perf_bins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        debrief_id INTEGER, boat_id TEXT, race_id TEXT, created_at REAL,
        tws REAL, twa REAL, point_of_sail TEXT, samples INTEGER,
        best_stw REAL, target_stw REAL, pct REAL);
    CREATE TABLE IF NOT EXISTS proposals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL, boat_id TEXT, status TEXT,
        helm_current REAL, helm_proposed REAL, overall_pct REAL,
        n_debriefs INTEGER, n_bins INTEGER,
        adjustments_json TEXT, summary_json TEXT,
        decided_at REAL, decided_note TEXT, applied_json TEXT);
    """)
    _ensure_columns(c)      # additive-only migration for archives created before these columns
    return c


# Columns added after the first schema — SQLite CREATE-IF-NOT-EXISTS won't add them to an existing
# table, so ALTER them in idempotently (additive, never destructive — the DB-safety ethos).
_ADDED = {
    "debriefs": [("helm_pct", "REAL"), ("sea_state_hs_mean", "REAL")],
    "perf_bins": [("hs_mean", "REAL"), ("pct_flat", "REAL"), ("config", "TEXT")],
    "proposals": [("kind", "TEXT DEFAULT 'boat_model'"), ("wave_json", "TEXT")],
}


def _ensure_columns(c):
    for table, cols in _ADDED.items():
        have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    c.commit()


# ---- archive (called by judge.run_judge after a debrief is scored) ----------------------------
def archive_debrief(report, boat_id=None):
    """Persist one debrief run to the ongoing archive. Idempotent-ish: every run is a row (a true
    log); proposals use only the LATEST run per race so re-runs don't double-count."""
    if not report or not report.get("available"):
        return None
    reg = report.get("regret") or {}
    at = report.get("actual_track") or {}
    crit = report.get("critique") or {}
    # keep the stored report lean (drop the heavy oracle path / windfield arrays)
    slim = {k: report.get(k) for k in ("race_id", "race_name", "playbook_id", "start_epoch",
                                       "regret", "playbook", "caveat")}
    slim["oracle"] = {k: (report.get("oracle") or {}).get(k)
                      for k in ("total_hours", "favored_side", "tacks")}
    slim["actual_track"] = {k: v for k, v in at.items() if k != "perf_bins"}
    slim["critique"] = crit
    c = _conn()
    try:
        cur = c.execute(
            """INSERT INTO debriefs (created_at,race_id,race_name,playbook_id,boat_id,oracle_hours,
               regret_min,side_paid,recommended_side,side_matched,track_source,elapsed_hours,
               time_behind_min,oversail_pct,xte_mean,xte_p90,xte_max,side_worked,polar_pct,
               polar_samples,report_json,helm_pct,sea_state_hs_mean)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), report.get("race_id"), report.get("race_name"), report.get("playbook_id"),
             boat_id, (report.get("oracle") or {}).get("total_hours"), reg.get("minutes"),
             reg.get("side_paid"), reg.get("recommended_side"), 1 if reg.get("side_matched") else 0,
             at.get("source"), at.get("elapsed_hours"), at.get("time_behind_optimal_min"),
             at.get("extra_distance_pct"), at.get("xte_mean_nm"), at.get("xte_p90_nm"),
             at.get("xte_max_nm"), at.get("side_worked"), at.get("polar_pct"),
             at.get("polar_samples"), json.dumps(slim), at.get("helm_pct"),
             at.get("sea_state_hs_mean")))
        did = cur.lastrowid
        for b in (at.get("perf_bins") or []):
            c.execute("""INSERT INTO perf_bins (debrief_id,boat_id,race_id,created_at,tws,twa,
                         point_of_sail,samples,best_stw,target_stw,pct,hs_mean,pct_flat,config)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (did, boat_id, report.get("race_id"), time.time(), b["tws"], b["twa"],
                       b["point_of_sail"], b["samples"], b["best_stw"], b["target_stw"], b["pct"],
                       b.get("hs_mean"), b.get("pct_flat"), b.get("config")))
        c.commit()
        return did
    finally:
        c.close()


def list_debriefs(boat_id=None, race_id=None, limit=200):
    c = _conn()
    try:
        q = ("SELECT id,created_at,race_id,race_name,boat_id,oracle_hours,regret_min,side_paid,"
             "recommended_side,side_matched,track_source,elapsed_hours,time_behind_min,oversail_pct,"
             "xte_mean,side_worked,polar_pct,helm_pct,sea_state_hs_mean,polar_samples FROM debriefs")
        cond, args = [], []
        if boat_id:
            cond.append("boat_id=?"); args.append(boat_id)
        if race_id:
            cond.append("race_id=?"); args.append(race_id)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def get_debrief(debrief_id):
    c = _conn()
    try:
        r = c.execute("SELECT * FROM debriefs WHERE id=?", (debrief_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        try:
            d["report"] = json.loads(d.pop("report_json") or "{}")
        except ValueError:
            d["report"] = {}
        d["perf_bins"] = [dict(x) for x in
                          c.execute("SELECT tws,twa,point_of_sail,samples,best_stw,target_stw,pct "
                                    "FROM perf_bins WHERE debrief_id=? ORDER BY tws,twa", (debrief_id,))]
        return d
    finally:
        c.close()


# ---- proposal engine (PROPOSES only — never applies) ------------------------------------------
def _latest_bins_per_race(c, boat_id):
    """The perf_bins from the LATEST debrief of each race for this boat (so repeated debrief runs of
    one race don't double-count). Returns (bins[], race_ids set)."""
    rows = c.execute("""SELECT pb.* FROM perf_bins pb JOIN (
                          SELECT race_id, MAX(debrief_id) AS did FROM perf_bins
                          WHERE boat_id IS ? GROUP BY race_id) latest
                        ON pb.debrief_id = latest.did""", (boat_id,)).fetchall()
    return [dict(r) for r in rows], {r["race_id"] for r in rows}


def config_polars(boat_id=None):
    """OBSERVED polars BY SAIL CONFIGURATION — the innovation record. Aggregates the archived
    per-config performance bins (boat-log debriefs; the crew's sails-bar history attributes every
    fix to its configuration): for each config × (TWS,TWA) cert cell, the best observed STW and
    sample depth across all races. Combinations the crossover chart doesn't rate (C0+J2,
    kite+staysail…) accumulate their own curves here over time. Read-only."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT config, tws, twa, point_of_sail, best_stw, target_stw, pct, samples, race_id "
            "FROM perf_bins WHERE config IS NOT NULL AND boat_id IS ? ORDER BY config, tws, twa",
            (boat_id,)).fetchall()
    finally:
        c.close()
    configs = {}
    for r in rows:
        r = dict(r)
        g = configs.setdefault(r["config"], {"config": r["config"], "cells": {}, "samples": 0,
                                             "races": set()})
        g["samples"] += r["samples"] or 0
        g["races"].add(r["race_id"])
        k = (r["tws"], r["twa"])
        cell = g["cells"].get(k)
        if cell is None or (r["best_stw"] or 0) > (cell["best_stw"] or 0):
            g["cells"][k] = {"tws": r["tws"], "twa": r["twa"], "point_of_sail": r["point_of_sail"],
                             "best_stw": r["best_stw"], "target_stw": r["target_stw"],
                             "pct": r["pct"], "samples": r["samples"]}
        elif cell is not None:
            cell["samples"] = (cell["samples"] or 0) + (r["samples"] or 0)
    out = []
    for g in configs.values():
        cells = sorted(g["cells"].values(), key=lambda x: (x["tws"], x["twa"]))
        out.append({"config": g["config"], "n_cells": len(cells), "samples": g["samples"],
                    "races": len(g["races"]), "cells": cells})
    out.sort(key=lambda g: -g["samples"])
    return {"configs": out,
            "note": ("Observed best-achievable STW per cert (TWS,TWA) cell, split by the crew's "
                     "sail CONFIGURATION (the sails-bar log). Small samples are anecdotes, not "
                     "polars — the record grows race by race.")}


def propose(boat_id):
    """Aggregate the archive → a PROPOSED boat-model refinement (helm_factor + per-cell polar overlay),
    written as a `proposed` row for human review. Never touches the boat profile."""
    from . import boats
    c = _conn()
    try:
        bins, races = _latest_bins_per_race(c, boat_id)
        if not bins:
            return {"ok": False, "note": "no archived performance bins for this boat yet — run a "
                    "debrief with a boat track first"}
        # Refine off the FLAT-WATER-equivalent % (pct_flat = raw pct with the sea-state loss removed)
        # so helm_factor stays a flat-water number and doesn't double-count waves; older bins with no
        # pct_flat fall back to the raw pct (== flat when there was no sea-state field).
        _pf = lambda b: (b["pct_flat"] if b.get("pct_flat") is not None else b["pct"])
        # overall achievable fraction = sample-weighted flat-water % of polar
        sw = sum(_pf(b) * b["samples"] for b in bins)
        sn = sum(b["samples"] for b in bins)
        overall_pct = sw / sn if sn else 100.0
        helm_current = float((boats.active_boat() or {}).get("helm_factor", 1.0)) \
            if (boats.active_boat() or {}).get("boat_id") == boat_id else 1.0
        helm_proposed = round(max(_HELM_MIN, min(_HELM_MAX, overall_pct / 100.0)), 3)

        # per-cell SHAPE multiplier, RELATIVE to the overall level (helm_factor carries the level, so
        # the overlay only captures where the boat is relatively weak/strong by angle — no double-count)
        cells = {}
        for b in bins:
            k = (b["tws"], b["twa"])
            cells.setdefault(k, {"pct": [], "samples": 0, "races": set(), "pos": b["point_of_sail"]})
            cells[k]["pct"].append(_pf(b)); cells[k]["samples"] += b["samples"]
            cells[k]["races"].add(b["race_id"])
        adjustments, by_pos = [], {}
        for (tws, twa), v in sorted(cells.items()):
            if len(v["races"]) < _MIN_RACES_FOR_CELL:
                continue
            cell_pct = sum(v["pct"]) / len(v["pct"])
            rel = (cell_pct / 100.0) / (overall_pct / 100.0) if overall_pct else 1.0
            mult = round(max(_CELL_MULT_MIN, min(_CELL_MULT_MAX, rel)), 3)
            if abs(mult - 1.0) < _CELL_DEADBAND:
                continue
            adjustments.append({"tws": tws, "twa": twa, "point_of_sail": v["pos"], "mult": mult,
                                "cell_pct": round(cell_pct), "samples": v["samples"],
                                "races": len(v["races"]),
                                "basis": f"sailed {round(cell_pct)}% of polar over {v['samples']} samples / "
                                         f"{len(v['races'])} race(s)"})
            p = by_pos.setdefault(v["pos"], {"pct": [], "n": 0})
            p["pct"].append(cell_pct); p["n"] += v["samples"]
        summary = {"overall_pct": round(overall_pct), "n_samples": sn,
                   "by_point_of_sail": {k: round(sum(x["pct"]) / len(x["pct"]))
                                        for k, x in by_pos.items()},
                   "races": sorted(races)}
        row = c.execute(
            """INSERT INTO proposals (created_at,boat_id,status,helm_current,helm_proposed,overall_pct,
               n_debriefs,n_bins,adjustments_json,summary_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), boat_id, "proposed", helm_current, helm_proposed, round(overall_pct, 1),
             len(races), len(bins), json.dumps(adjustments), json.dumps(summary)))
        c.commit()
        return {"ok": True, **get_proposal(row.lastrowid)}
    finally:
        c.close()


def _row_to_proposal(r):
    d = dict(r)
    for k in ("adjustments_json", "summary_json", "applied_json", "wave_json"):
        v = d.pop(k, None)
        default = None if k in ("applied_json", "wave_json") else []
        d[k.replace("_json", "")] = json.loads(v) if v else default
    d.setdefault("kind", "boat_model")
    if d.get("kind") is None:
        d["kind"] = "boat_model"
    return d


def get_proposal(pid):
    c = _conn()
    try:
        r = c.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return _row_to_proposal(r) if r else None
    finally:
        c.close()


def list_proposals(boat_id=None, limit=50):
    c = _conn()
    try:
        q = "SELECT * FROM proposals"
        args = []
        if boat_id:
            q += " WHERE boat_id=?"; args.append(boat_id)
        q += " ORDER BY created_at DESC LIMIT ?"; args.append(limit)
        return [_row_to_proposal(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def apply_proposal(pid, helm_factor=None, adjustments=None, note="", wave_coeffs=None):
    """HUMAN-APPROVED apply: write the (possibly human-edited) refinement onto the boat profile and mark
    the proposal applied. Falls back to the proposed values when the caller doesn't override. This is
    the ONLY path that mutates the boat's model — gated behind a person. A `wave_coeffs`-kind proposal
    writes the sea-state coefficients; a `boat_model`-kind writes helm_factor + the polar overlay."""
    from . import boats
    prop = get_proposal(pid)
    if not prop:
        return {"ok": False, "note": "unknown proposal"}
    if prop["status"] != "proposed":
        return {"ok": False, "note": f"proposal already {prop['status']}"}
    boat = boats.get_boat(prop["boat_id"])
    if not boat:
        return {"ok": False, "note": "boat profile not found"}
    if prop.get("kind") == "wave_coeffs":
        return _apply_wave(prop, boat, wave_coeffs, note)
    hf = prop["helm_proposed"] if helm_factor is None else float(helm_factor)
    hf = round(max(_HELM_MIN, min(_HELM_MAX, hf)), 3)
    adj = prop["adjustments"] if adjustments is None else adjustments
    clean = [{"tws": a["tws"], "twa": a["twa"],
              "mult": round(max(_CELL_MULT_MIN, min(_CELL_MULT_MAX, float(a["mult"]))), 3),
              "basis": a.get("basis", "")}
             for a in (adj or []) if a.get("mult") is not None]
    boat["helm_factor"] = hf
    boat["polar_adjustments"] = clean
    boats.save_boat(boat)
    applied = {"helm_factor": hf, "polar_adjustments": clean}
    c = _conn()
    try:
        c.execute("UPDATE proposals SET status='applied',decided_at=?,decided_note=?,applied_json=? "
                  "WHERE id=?", (time.time(), note, json.dumps(applied), pid))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "boat_id": boat["boat_id"], "applied": applied}


def _apply_wave(prop, boat, wave_coeffs, note):
    """HUMAN-APPROVED apply of a wave-coefficient proposal → boat.wave_coeffs (the sea-state overlay)."""
    from . import boats
    coeffs = dict(wave_coeffs) if wave_coeffs else dict(prop.get("wave") or {})
    if not coeffs:
        return {"ok": False, "note": "no wave coefficients to apply"}
    clean = {"hs_deadband": round(float(coeffs.get("hs_deadband", 0.5)), 3),
             "floor": round(float(coeffs.get("floor", 0.6)), 3)}
    for key in ("k_up", "k_reach", "k_down"):
        if coeffs.get(key) is not None:
            clean[key] = round(max(_WAVE_K_MIN, min(_WAVE_K_MAX, float(coeffs[key]))), 4)
    boat["wave_coeffs"] = clean
    boats.save_boat(boat)
    c = _conn()
    try:
        c.execute("UPDATE proposals SET status='applied',decided_at=?,decided_note=?,applied_json=? "
                  "WHERE id=?", (time.time(), note, json.dumps({"wave_coeffs": clean}), prop["id"]))
        c.commit()
    finally:
        c.close()
    return {"ok": True, "boat_id": boat["boat_id"], "applied": {"wave_coeffs": clean}}


def reject_proposal(pid, note=""):
    c = _conn()
    try:
        r = c.execute("SELECT status FROM proposals WHERE id=?", (pid,)).fetchone()
        if not r:
            return {"ok": False, "note": "unknown proposal"}
        if r["status"] != "proposed":
            return {"ok": False, "note": f"proposal already {r['status']}"}
        c.execute("UPDATE proposals SET status='rejected',decided_at=?,decided_note=? WHERE id=?",
                  (time.time(), note, pid))
        c.commit()
        return {"ok": True}
    finally:
        c.close()


# ---- wave-coefficient calibration (Lab-4 condition attribution) --------------------------------
def _wls_line(pts):
    """Sample-weighted least-squares fit y = b0 + b1·x over pts=[(x,y,w)]. Returns (b0,b1,r2) or None
    when x has no spread (a vertical line can't be fit)."""
    W = sum(w for _x, _y, w in pts)
    if W <= 0:
        return None
    mx = sum(w * x for x, _y, w in pts) / W
    my = sum(w * y for _x, y, w in pts) / W
    sxx = sum(w * (x - mx) ** 2 for x, _y, w in pts)
    sxy = sum(w * (x - mx) * (y - my) for x, y, w in pts)
    if sxx <= 1e-9:
        return None
    b1 = sxy / sxx
    b0 = my - b1 * mx
    sst = sum(w * (y - my) ** 2 for _x, y, w in pts)
    ssr = sum(w * (y - (b0 + b1 * x)) ** 2 for x, y, w in pts)
    r2 = (1 - ssr / sst) if sst > 1e-9 else 0.0
    return b0, b1, max(0.0, min(1.0, r2))


def _fit_deadband(by_pos_raw):
    """Global knee (deadband) fit across all points of sail from raw (hs, ratio, samples) points.

    For each candidate knee db, each point of sail is a CONTINUOUS hinge — ratio = helm − k·eff with
    eff = max(0, hs − db) — fit by one weighted OLS over all that pos's points; we pick the db minimising
    the total weighted residual (strict, so a genuinely-better knee wins ties). The continuous hinge (vs
    two free segments) is what makes the knee identifiable: below the true knee a still-flat point pulled
    past db deviates from the single fitted line, so only the true db drives the residual to zero. Needs a
    clear flat region (eff-0 points) AND a sloped region on BOTH sides, else returns None (hold the prior).
    Returns (db, n_flat, n_sloped)."""
    best = None
    for db in _WAVE_DB_GRID:
        ssr = wtot = 0.0
        nlow = nhigh = 0
        for pts in by_pos_raw.values():
            effs = [(max(0.0, h - db), r, w) for h, r, w in pts]
            low = [e for e in effs if e[0] <= 1e-9]
            high = [e for e in effs if e[0] > 1e-9]
            if not low or len(high) < 2:
                continue                       # this pos can't constrain the knee at this db
            fit = _wls_line(effs)              # one hinge line over flat + sloped points
            if not fit:
                continue
            b0, b1, _r2 = fit
            nlow += len(low); nhigh += len(high)
            for e, r, w in effs:
                ssr += w * (r - (b0 + b1 * e)) ** 2; wtot += w
        if nlow < _WAVE_DB_MIN_LOW or nhigh < _WAVE_DB_MIN_HIGH or wtot <= 0:
            continue
        score = ssr / wtot
        if best is None or score < best[1] - 1e-12:   # strict → ties keep the smaller (earlier) knee
            best = (db, score, nlow, nhigh)
    return (best[0], best[2], best[3]) if best else None


def _current_wave_coeffs(boat_id):
    """The coefficients the optimizer is using now — the boat's wave_coeffs overlay if present, else the
    ROUTE_WAVE_* env priors (mirrors optimizer._wave_factor's fallback)."""
    from . import boats
    b = boats.get_boat(boat_id) or {}
    env = {"hs_deadband": float(os.environ.get("ROUTE_WAVE_HS_DEADBAND", "0.5")),
           "k_up": float(os.environ.get("ROUTE_WAVE_K_UP", "0.04")),
           "k_reach": float(os.environ.get("ROUTE_WAVE_K_REACH", "0.025")),
           "k_down": float(os.environ.get("ROUTE_WAVE_K_DOWN", "0.01")),
           "floor": float(os.environ.get("ROUTE_WAVE_FLOOR", "0.6"))}
    env.update({k: float(v) for k, v in (b.get("wave_coeffs") or {}).items() if v is not None})
    return env


def calibrate_waves(boat_id):
    """Fit the sea-state degradation slope k (per point of sail) from the archive → a PROPOSED
    `wave_coeffs` refinement for human review. Never mutates the boat.

    Model: the boat's RAW %-of-flat-polar in a cell = helm_level × wave_factor(Hs) where wave_factor =
    1 − k·eff (eff = max(0, Hs − deadband)). Per point of sail this is linear in eff, so a weighted
    least-squares of ratio(=pct/100) on eff gives intercept b0 = helm_level and slope b1 = −helm·k →
    **k = −b1/b0**. The DEADBAND (knee) is also fit globally when the archive has data on both sides of
    it (a flat low-Hs region + a sloped region), else the prior deadband is held; the FLOOR stays the
    conservative prior (hitting it needs extreme seas). A point of sail's prior k is kept when its data
    lacks Hs SPREAD (can't fit a slope from flat-water races)."""
    cur = _current_wave_coeffs(boat_id)
    floor = cur["floor"]
    c = _conn()
    try:
        bins, races = _latest_bins_per_race(c, boat_id)
    finally:
        c.close()
    have_hs = [b for b in bins if b.get("hs_mean") is not None and b.get("pct") is not None]
    if not have_hs:
        return {"ok": False, "note": "no archived sea-state data yet — run debriefs with a boat track "
                "in real waves first (flat-water races carry no Hs to calibrate against)"}
    # raw (hs, ratio, samples) per point of sail — keep raw Hs so the deadband knee can be searched
    by_pos_raw = {}
    for b in have_hs:
        by_pos_raw.setdefault(b["point_of_sail"], []).append(
            (float(b["hs_mean"]), float(b["pct"]) / 100.0, b["samples"]))
    # fit the deadband (knee) globally if the data supports it; else hold the prior
    db_fit = _fit_deadband(by_pos_raw)
    db = round(max(_WAVE_DB_GRID[0], min(_WAVE_DB_GRID[-1], db_fit[0])), 2) if db_fit else cur["hs_deadband"]
    db_source = "fit" if db_fit else "prior"
    by_pos = {pos: [(max(0.0, h - db), r, w) for h, r, w in pts] for pos, pts in by_pos_raw.items()}
    proposed = {"hs_deadband": round(db, 3), "floor": round(floor, 3)}
    db_info = {"current": round(cur["hs_deadband"], 3), "proposed": round(db, 3), "source": db_source,
               "note": ("fit the knee from a flat + sloped region" if db_fit else
                        "held prior — no clear flat/sloped split in the archive to locate the knee"),
               "floor": round(floor, 3), "floor_note": "held prior (extreme-sea data absent)"}
    detail = {}
    fitted_any = False
    for pos, key in _WAVE_POS_KEY.items():
        pts = by_pos.get(pos, [])
        k_cur = cur[key]
        effs = [x for x, _y, _w in pts]
        spread = (max(effs) - min(effs)) if effs else 0.0
        n = len(pts)
        d = {"point_of_sail": pos, "k_current": round(k_cur, 4), "n_cells": n,
             "samples": sum(w for _x, _y, w in pts), "hs_spread": round(spread, 2)}
        fit = _wls_line(pts) if (n >= _WAVE_MIN_CELLS and spread >= _WAVE_MIN_HS_SPREAD) else None
        if fit and fit[0] > 0.2:                       # need a sane helm intercept to divide by
            b0, b1, r2 = fit
            k_raw = -b1 / b0
            k_new = round(max(_WAVE_K_MIN, min(_WAVE_K_MAX, k_raw)), 4)
            proposed[key] = k_new
            d.update({"k_proposed": k_new, "k_raw": round(k_raw, 4), "helm_level": round(b0, 3),
                      "r2": round(r2, 2),
                      "confidence": round(min(1.0, (n / 8.0) * min(1.0, spread / 1.5)) * (0.5 + 0.5 * r2), 2)})
            fitted_any = True
        else:
            proposed[key] = round(k_cur, 4)            # keep the prior — not enough spread to fit
            d.update({"k_proposed": round(k_cur, 4), "confidence": 0.0,
                      "note": ("insufficient sea-state spread — need ≥%d cells spanning ≥%.1f m Hs "
                               "(have %d cells / %.1f m)" % (_WAVE_MIN_CELLS, _WAVE_MIN_HS_SPREAD, n, spread))})
        detail[pos] = d
    if not fitted_any and db_source != "fit":
        return {"ok": False, "note": "not enough sea-state SPREAD in the archive to fit the wave model "
                "yet — needs races sailed across a range of wave heights.", "by_point_of_sail": detail,
                "deadband": db_info, "current_coeffs": {k: round(v, 4) for k, v in cur.items()}}
    summary = {"by_point_of_sail": detail, "deadband": db_info,
               "current_coeffs": {k: round(v, 4) for k, v in cur.items()}, "races": sorted(races)}
    c = _conn()
    try:
        row = c.execute(
            """INSERT INTO proposals (created_at,boat_id,status,kind,helm_current,helm_proposed,
               overall_pct,n_debriefs,n_bins,adjustments_json,summary_json,wave_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), boat_id, "proposed", "wave_coeffs", None, None, None,
             len(races), len(have_hs), json.dumps([]), json.dumps(summary), json.dumps(proposed)))
        c.commit()
        return {"ok": True, **get_proposal(row.lastrowid)}
    finally:
        c.close()


# ---- multi-race trend (Lab-4 condition attribution) --------------------------------------------
def trend(boat_id, limit=50):
    """The boat's per-race performance series (latest debrief per race, oldest→newest) + the applied
    boat-model/wave refinements as milestone markers — so you can SEE the model improving over a
    season: helm_pct trending up, regret/time-behind shrinking, and where an approved refinement bit."""
    c = _conn()
    try:
        rows = c.execute(
            """SELECT d.* FROM debriefs d JOIN (
                 SELECT race_id, MAX(id) AS mid FROM debriefs
                 WHERE boat_id IS ? GROUP BY race_id) latest
               ON d.id = latest.mid ORDER BY d.created_at ASC LIMIT ?""", (boat_id, limit)).fetchall()
        series = []
        for r in rows:
            d = dict(r)
            series.append({k: d.get(k) for k in (
                "id", "created_at", "race_id", "race_name", "elapsed_hours", "oracle_hours",
                "time_behind_min", "oversail_pct", "xte_mean", "side_matched", "polar_pct",
                "helm_pct", "sea_state_hs_mean", "regret_min")})
        applied = c.execute(
            """SELECT id,created_at,decided_at,kind,helm_proposed,applied_json FROM proposals
               WHERE boat_id IS ? AND status='applied' ORDER BY decided_at ASC""", (boat_id,)).fetchall()
        milestones = []
        for r in applied:
            m = dict(r)
            try:
                m["applied"] = json.loads(m.pop("applied_json") or "{}")
            except ValueError:
                m["applied"] = {}
            milestones.append(m)
        return {"boat_id": boat_id, "n_races": len(series), "series": series, "milestones": milestones}
    finally:
        c.close()
