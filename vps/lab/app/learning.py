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
    return c


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
               polar_samples,report_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), report.get("race_id"), report.get("race_name"), report.get("playbook_id"),
             boat_id, (report.get("oracle") or {}).get("total_hours"), reg.get("minutes"),
             reg.get("side_paid"), reg.get("recommended_side"), 1 if reg.get("side_matched") else 0,
             at.get("source"), at.get("elapsed_hours"), at.get("time_behind_optimal_min"),
             at.get("extra_distance_pct"), at.get("xte_mean_nm"), at.get("xte_p90_nm"),
             at.get("xte_max_nm"), at.get("side_worked"), at.get("polar_pct"),
             at.get("polar_samples"), json.dumps(slim)))
        did = cur.lastrowid
        for b in (at.get("perf_bins") or []):
            c.execute("""INSERT INTO perf_bins (debrief_id,boat_id,race_id,created_at,tws,twa,
                         point_of_sail,samples,best_stw,target_stw,pct) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (did, boat_id, report.get("race_id"), time.time(), b["tws"], b["twa"],
                       b["point_of_sail"], b["samples"], b["best_stw"], b["target_stw"], b["pct"]))
        c.commit()
        return did
    finally:
        c.close()


def list_debriefs(boat_id=None, race_id=None, limit=200):
    c = _conn()
    try:
        q = ("SELECT id,created_at,race_id,race_name,boat_id,oracle_hours,regret_min,side_paid,"
             "recommended_side,side_matched,track_source,elapsed_hours,time_behind_min,oversail_pct,"
             "xte_mean,side_worked,polar_pct,polar_samples FROM debriefs")
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
        # overall achievable fraction = sample-weighted % of polar
        sw = sum(b["pct"] * b["samples"] for b in bins)
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
            cells[k]["pct"].append(b["pct"]); cells[k]["samples"] += b["samples"]
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
    for k in ("adjustments_json", "summary_json", "applied_json"):
        v = d.pop(k, None)
        d[k.replace("_json", "")] = json.loads(v) if v else (None if k == "applied_json" else [])
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


def apply_proposal(pid, helm_factor=None, adjustments=None, note=""):
    """HUMAN-APPROVED apply: write the (possibly human-edited) helm_factor + polar overlay onto the
    boat profile, and mark the proposal applied. Falls back to the proposed values when the caller
    doesn't override. This is the ONLY path that mutates the boat's polars — gated behind a person."""
    from . import boats
    prop = get_proposal(pid)
    if not prop:
        return {"ok": False, "note": "unknown proposal"}
    if prop["status"] != "proposed":
        return {"ok": False, "note": f"proposal already {prop['status']}"}
    boat = boats.get_boat(prop["boat_id"])
    if not boat:
        return {"ok": False, "note": "boat profile not found"}
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
