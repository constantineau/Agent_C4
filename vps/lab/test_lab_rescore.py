"""Re-score the archive with today's evidence code as a versioned run (#245).

Cole, 2026-11-09: keep the applied refinements, re-score the archived debriefs with a versioned
run, compare, and he approves any replacement (ADR-0003). Before this there was no way to do that:
`propose()` only re-aggregated the archived (flawed) bins, and re-running a debrief appended new
perf_bins that `propose()` then read as the archive. Every block fails on the old code (the
re-score module, `learning.refine` and `judge.score_recording` did not exist).

1. The proposal math is a pure function and `propose()` still gives the same answer through it.
2. A re-score run scores the archived recordings again, compares per cell with the boat model in
   use, and writes ONLY its own row: no perf_bins, no proposals, no boat change.
3. A recording the Lab does not store is fetched in memory, or left out and counted.
4. Approving a run files an ordinary `proposed` row; the boat still does not change. A decided run
   cannot be decided again.
5. `score_recording` builds current and sea-state fields over the recording's own time (#68).
6. The Polar tab shows the comparison and its buttons only propose or reject.

Run in the CI image:
  docker run --rm --network none -v "$PWD:/repo:ro" -w /srv -e PYTHONPATH=/repo/vps/lab:/repo \
    -e C4_CI=1 sr33-ci-lab python3 /repo/vps/lab/test_lab_rescore.py
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

from app import boats
from app import judge as J
from app import learning as LN
from app import track as T

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


tmp = tempfile.mkdtemp()
LN.LEARNING_DB = os.path.join(tmp, "learning.db")
boats.EDITED_DIR = os.path.join(tmp, "boats")
boats.BUNDLED_DIR = os.path.join(tmp, "bundled")
T.TRACK_DIR = os.path.join(tmp, "tracks")
os.makedirs(boats.EDITED_DIR)
os.makedirs(T.TRACK_DIR)

W15, W18 = 1784155457.9, 1784394211.4      # the two Bayview recordings' window starts
RID = "bayview-mackinac-2026"


def b(tws, twa, pct, n, src="measured", pos="upwind"):
    return {"tws": tws, "twa": twa, "point_of_sail": pos, "samples": n, "best_stw": 5.0,
            "target_stw": 5.0, "pct": pct, "wind_source": src, "config": "J1"}


def archive(ws, bins, source="boatlog"):
    return LN.archive_debrief({"available": True, "race_id": RID, "race_name": "Bayview",
                               "regret": {},
                               "actual_track": {"available": True, "source": source,
                                                "window": ({"start_ts": ws} if ws else None),
                                                "polar_pct": 90, "perf_bins": bins}}, "sr33")


def rows(table):
    c = sqlite3.connect(LN.LEARNING_DB)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# ---- 1) the proposal math is pure, and propose() still gives the same answer -------------------
print("1) refine() is propose()'s math")
OLD15 = [b(6.0, 52.0, 60, 40), b(10.0, 110.0, 80, 60, pos="reaching"), b(12.0, 37.0, 100, 50)]
OLD18 = [b(6.0, 52.0, 70, 30), b(16.0, 36.0, 104, 200), b(4.0, 135.0, 120, 25, pos="downwind"),
         b(8.0, 70.0, 90, 30, src="inferred")]
archive(W15, OLD15)
archive(W18, OLD18)
boat = {"boat_id": "sr33", "name": "SR33", "helm_factor": 1.0}
boats.save_boat(boat)
boats.active_boat = lambda: boats.get_boat("sr33")
pure = LN.refine([{**x, "race_id": RID, "window_start": W15} for x in OLD15]
                 + [{**x, "race_id": RID, "window_start": W18} for x in OLD18], None, 1.0)
prop = LN.propose("sr33")
check("propose() files a proposal", prop.get("ok") and prop["status"] == "proposed")
check(f"refine() gives the same helm ({pure.get('helm_proposed')} vs {prop.get('helm_proposed')})",
      pure.get("ok") and pure["helm_proposed"] == prop["helm_proposed"])
check("…and the same overlay cells",
      [(a["tws"], a["twa"], a["mult"]) for a in pure["adjustments"]]
      == [(a["tws"], a["twa"], a["mult"]) for a in prop["adjustments"]])
check("refine() also reports every cell's evidence and why it was or wasn't proposed",
      {c["outcome"] for c in pure["cells"]} >= {"proposed"} and all("samples" in c for c in pure["cells"]))

# apply it as Cole did, leaving one proposed cell out (he unchecked five on 2026-09-16)
keep = [a for a in prop["adjustments"] if not (a["tws"] == 4.0)]
left = [a for a in prop["adjustments"] if a["tws"] == 4.0]
LN.apply_proposal(prop["id"], adjustments=keep, note="test apply")
applied_boat = json.dumps(boats.get_boat("sr33"), sort_keys=True)
check("setup: one proposed cell (4 kn) was left out of the apply", len(left) == 1)

# ---- 2) a re-score run: compare, write only its own row --------------------------------------
print("2) a re-score run compares with the boat in use and writes only its own row")
from app import rescore as RS                       # noqa: E402 — the module this ticket adds

# a stored track for Jul 18 only (as on the live Lab), Jul 15 comes from the boat log
T.save_track(RID, {"source": "boatlog", "window": {"start_ts": W18, "end_ts": W18 + 3600},
                   "fixes": [{"t": W18, "lat": 45.0, "lon": -83.0}] * 3})
NEW = {   # what today's code makes of each recording
    W15: [b(6.0, 52.0, 84, 40), b(10.0, 110.0, 81, 60, pos="reaching"), b(12.0, 37.0, 99, 50)],
    W18: [b(6.0, 52.0, 86, 30), b(16.0, 36.0, 104, 200), b(4.0, 135.0, 99, 25, pos="downwind"),
          b(8.0, 70.0, 88, 30, src="inferred"), b(20.0, 110.0, 80, 40, pos="reaching")],
}
seen = []


def fake_score(t, wave_coeffs, log):
    ws = t["window"]["start_ts"]
    seen.append((round(ws, 1), t.get("source")))
    return {"available": True, "perf_bins": NEW[min(NEW, key=lambda k: abs(k - ws))],
            "polar_pct": 91, "elapsed_hours": 1.0, "current_corrected": True,
            "wave_corrected": True, "trust": {"refused_samples": 0}}


fetched = []


def fake_fetch(race_id, ws):
    fetched.append(ws)
    return {"source": "boatlog", "window": {"start_ts": ws, "end_ts": ws + 3600},
            "fixes": [{"t": ws}] * 3}, None


n_bins, n_props = rows("perf_bins"), rows("proposals")
tracks_before = sorted(os.listdir(T.TRACK_DIR))
run = RS.run("sr33", fetch_track=fake_fetch, score=fake_score)
check("the run is saved, versioned, and ready for a decision",
      run and run["status"] == "ready" and run["evidence_version"] == RS.EVIDENCE_VERSION)
check(f"both recordings were scored (got {seen})", len(seen) == 2)
check("Jul 18 used the stored track, Jul 15 came from the boat log",
      [r.get("track") for r in run["recordings"]] == ["boat log", "stored"] and len(fetched) == 1)
check("the fetched track was not saved to the track store", sorted(os.listdir(T.TRACK_DIR)) == tracks_before)
check("no perf_bins were written", rows("perf_bins") == n_bins)
check("no proposal was filed", rows("proposals") == n_props)
check("the boat model is unchanged", json.dumps(boats.get_boat("sr33"), sort_keys=True) == applied_boat)
cmp_ = run["comparison"]
by = {(r["tws"], r["twa"]): r for r in cmp_["rows"]}
check("comparison is available and counts each status",
      cmp_["available"] and set(cmp_["counts"]) == {"same", "changes", "dropped", "new"})
check(f"helm: now {cmp_['helm']['now']} vs new {cmp_['helm']['new']}",
      cmp_["helm"]["now"] == boats.get_boat("sr33")["helm_factor"] and cmp_["helm"]["new"] is not None)
r6 = by.get((6.0, 52.0))
check(f"6 kn/52°: applied and re-scored side by side with evidence (got {r6})",
      r6 and r6["now"] is not None and r6["new"] is not None and r6["status"] == "changes"
      and r6["now_samples"] == 70 and r6["new_samples"] == 70 and r6["new_pct"] is not None)
r20 = by.get((20.0, 110.0))
check(f"20 kn/110°: a cell only the re-score proposes reads 'new' (got {r20 and r20['status']})",
      r20 and r20["status"] == "new" and r20["now"] is None)
r4 = by.get((4.0, 135.0))
check(f"4 kn/135°: the cell left out of the last apply is flagged if it comes back (got {r4})",
      r4 is None or r4["left_out_before"])
gone = [r for r in cmp_["rows"] if r["status"] == "dropped"]
check("a dropped cell says why (too little data / within 4% / no data)",
      all(r["why"] in ("thin", "deadband", "no_data", "few_races") for r in gone))
check("inferred bins stay out of the re-score as well",
      run["result"]["summary"]["excluded_inferred_bins"] == 1)
check("the run carries what the boat used when it ran",
      run["against"]["helm_factor"] == boats.get_boat("sr33")["helm_factor"]
      and run["against"]["proposal_id"] == prop["id"])
check("the latest run is served", LN.latest_rescore("sr33")["id"] == run["id"])

# ---- 3) a recording the Lab does not store ---------------------------------------------------
print("3) a recording with no track is left out and counted")
run2 = RS.run("sr33", fetch_track=None, score=fake_score)
miss = [r for r in run2["recordings"] if r["status"] == "left_out"]
check(f"with no boat log, Jul 15 is left out by name (got {[r.get('note') for r in miss]})",
      len(miss) == 1 and miss[0]["window_start"] and "not stored" in miss[0]["note"])
# an old GPX debrief's bare path now holds a boat-log recording: never score that as the GPX
archive(None, [b(8.0, 45.0, 101, 10, src=None)], source="gpx")
os.replace(T._path(RID, W18), T._path(RID))            # the pre-2026-09-15 layout
seen.clear()
run3 = RS.run("sr33", fetch_track=fake_fetch, score=fake_score)
gpx = [r for r in run3["recordings"] if r["window_start"] is None]
check(f"the GPX debrief whose track is gone is left out (got {gpx and gpx[0].get('note')})",
      gpx and gpx[0]["status"] == "left_out")
check("the legacy-path Jul 18 track is still scored once", sum(1 for s in seen if s[0] == round(W18, 1)) == 1)
check("log_body_for finds the session by its derived window",
      RS.log_body_for(W15, [{"id": 1, "start_ts": W15 - 300, "window": {"start_ts": W15}}])
      == {"session_id": 1, "start_ts": W15 - 300})
check("a recording archived on the marker re-scores the marker, not a moved derived window",
      RS.log_body_for(W15, [{"id": 1, "start_ts": W15, "window": {"start_ts": W15 + 600}}])
      == {"session_id": 1, "start_ts": W15, "use_marker": True})
off = RS._track_for({"race_id": RID, "window_start": W15},
                    lambda r, ws: ({"window": {"start_ts": ws + 600}, "fixes": []}, None))
check(f"a fetched track whose window is another recording is left out (got {off[1]})",
      off[0] is None and "differently" in off[1])
check("…and says None when the boat log no longer has it", RS.log_body_for(W15, [{"id": 9, "start_ts": 1.0}]) is None)

# ---- 4) approval only proposes ----------------------------------------------------------------
print("4) approve files a proposal; the boat changes only through apply")
n_props = rows("proposals")
res = LN.rescore_propose(run["id"], "Cole: try it")
check("approve files one proposal", res.get("ok") and rows("proposals") == n_props + 1)
p = LN.get_proposal(res["proposal_id"])
check("…an ordinary 'proposed' boat-model row, tagged with the run and scoring version",
      p["status"] == "proposed" and p["kind"] == "boat_model"
      and p["summary"]["rescore_run"] == run["id"]
      and p["summary"]["evidence_version"] == RS.EVIDENCE_VERSION)
check("…with the re-scored values", p["helm_proposed"] == run["result"]["helm_proposed"]
      and len(p["adjustments"]) == len(run["result"]["adjustments"]))
check("the boat model is still unchanged", json.dumps(boats.get_boat("sr33"), sort_keys=True) == applied_boat)
bt0 = boats.get_boat("sr33")
boats.save_boat({**bt0, "wave_coeffs": {"hs_deadband": 0.5, "k_up": 0.1, "floor": 0.6}})
check("applying a re-score suggestion after the wave settings changed is refused",
      LN.apply_proposal(res["proposal_id"])["ok"] is False
      and LN.get_proposal(res["proposal_id"])["status"] == "proposed")
boats.save_boat(bt0)
os.environ["ROUTE_WAVE_K_UP"] = "0.09"          # no overlay: the env prior is what changed
check("…also when only the env prior changed (no boat overlay)",
      LN.apply_proposal(res["proposal_id"])["ok"] is False)
del os.environ["ROUTE_WAVE_K_UP"]
check("the run is marked proposed", LN.get_rescore(run["id"])["status"] == "proposed")
check("a second approve is refused", LN.rescore_propose(run["id"])["ok"] is False
      and rows("proposals") == n_props + 1)
check("reject on a decided run is refused", LN.rescore_reject(run["id"])["ok"] is False)
check("reject on a ready run works", LN.rescore_reject(run2["id"])["ok"] is True
      and LN.get_rescore(run2["id"])["status"] == "rejected")
run4 = RS.run("sr33", fetch_track=fake_fetch, score=fake_score)
bt = boats.get_boat("sr33")
boats.save_boat({**bt, "wave_coeffs": {"hs_deadband": 0.5, "k_up": 0.1, "floor": 0.6}})
check("approve is refused when the wave settings changed since the run",
      LN.rescore_propose(run4["id"])["ok"] is False and LN.get_rescore(run4["id"])["status"] == "ready")
boats.save_boat(bt)
os.environ["ROUTE_WAVE_K_UP"] = "0.09"
check("…and when only the env prior changed", LN.rescore_propose(run4["id"])["ok"] is False)
del os.environ["ROUTE_WAVE_K_UP"]
check("approve on a rejected run is refused", LN.rescore_propose(run2["id"])["ok"] is False)

# a wave fit that succeeds is shown, never filed: helm stays on the coefficients in use
print("4b) a re-score's wave fit is shown, not filed")
WB = [{**b(10.0, 45.0, 0, 50), "hs_mean": hs, "race_id": RID, "window_start": W18}
      for hs in (0.2, 0.3, 0.4, 1.2, 1.6, 2.0)]
for x in WB:            # a clean slope: 90% flat, losing 6%/m above 0.5 m, scored on no wave loss
    x["pct"] = x["pct_flat"] = round(90 * (1 - 0.06 * max(0.0, x["hs_mean"] - 0.5)))
wr = LN.save_rescore("sr33", "t", [], WB)
check("the wave fit succeeds and is in the comparison",
      (wr["wave"] or {}).get("ok") and wr["comparison"]["wave"]["available"])
n_props = rows("proposals")
res_w = LN.rescore_propose(wr["id"])
check("approve files ONE proposal, the boat-model one, refined on the waves in use",
      res_w.get("ok") and rows("proposals") == n_props + 1
      and LN.get_proposal(res_w["proposal_id"])["kind"] == "boat_model")

# ---- 5) score_recording: fields over the recording's own time ---------------------------------
print("5) score_recording builds current and sea state for the recording's own span")
import app.current as CUR                             # noqa: E402
import app.wave as WAV                                # noqa: E402
spans = []
CUR.build_currentfield = lambda bbox, a, z, on_progress=None: spans.append(("cur", a, z)) or None
WAV.build_wavefield = lambda bbox, a, z, on_progress=None: spans.append(("wave", a, z)) or None
fx = [{"t": W15 + 10 * i, "lat": 45.0 + i * 1e-4, "lon": -83.0, "sog": 6.0, "cog": 0.0,
       "tws": 10.0, "twa": 52.0, "stw": 6.0} for i in range(40)]
s = J.score_recording({"source": "boatlog", "fixes": fx,
                       "window": {"start_ts": W15, "end_ts": W15 + 400}}, None)
check(f"both fields asked for over the window (got {spans})",
      ("cur", W15, W15 + 400) in spans and ("wave", W15, W15 + 400) in spans)
check("it scores the speed half and returns bins", s.get("available") and s.get("perf_bins"))

# ---- 6) the Polar tab --------------------------------------------------------------------------
print("6) Polar tab: a Re-score card that only proposes or rejects")
JS = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "app.js")).read()


def fn(name):
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", JS, re.M)
    if not m:
        return ""
    nxt = re.search(r"^(?:async )?function \w+\(|^const \w+ = |^let \w+ = ", JS[m.end():], re.M)
    return JS[m.start(): m.end() + (nxt.start() if nxt else len(JS))]


check("paintPolar shows the Re-score card", "polRescoreCard()" in fn("paintPolar"))
check("renderPolar loads the latest run", "/api/learning/rescore/latest" in fn("renderPolar"))
card = fn("polRescoreCard")
check("the card shows now vs new per cell with the evidence", all(k in card for k in (
    "r.now", "r.new", "now_samples", "new_samples", "Helm")))
dec = fn("polRescoreDecide")
check("approve posts to the run's propose route, never to apply",
      "/api/learning/rescore/${id}/${what}" in dec and "/apply" not in dec + card + fn("polRescoreStart"))
check("reject asks first", "askModal(" in dec and dec.index("askModal(") < dec.index("apiPost("))

print("\nALL OK" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
