"""Lab-4 learning loop — polar overlay + archive + PROPOSE/APPROVE/REJECT (human-in-the-loop).

Deterministic + offline. Asserts the human gate: propose() writes a proposal but never touches the
boat profile; only apply_proposal() (the human-approved path) mutates helm_factor + polar_adjustments.
Uses a temp learning DB + a throwaway boat, cleaned up after. Run in-container:
  docker cp vps/lab/test_routing_learning.py sr33-dev-lab-1:/srv/ && docker compose ... exec ... python test_routing_learning.py
"""
import os
import tempfile

from app import polars, learning, boats


def test_polar_overlay():
    P = [(8.0, 45.0, 6.0), (8.0, 90.0, 7.0), (12.0, 45.0, 6.5)]
    out = polars.apply_adjustments(P, [{"tws": 8.0, "twa": 45.0, "mult": 0.9},
                                       {"tws": 12.0, "twa": 45.0, "mult": 1.5}])  # clamps to 1.15
    d = {(t, a): s for (t, a, s) in out}
    assert abs(d[(8.0, 45.0)] - 5.4) < 1e-6, d              # 6.0 * 0.9
    assert abs(d[(8.0, 90.0)] - 7.0) < 1e-6, d              # untouched
    assert abs(d[(12.0, 45.0)] - 6.5 * 1.15) < 1e-6, d      # 1.5 clamped to 1.15
    assert polars.apply_adjustments(P, []) is P             # no-op returns the same object
    print("PASS polar_overlay: per-cell multipliers applied + clamped, no-op identity")


def _fake_report(race_id, bins, polar_pct):
    return {"available": True, "race_id": race_id, "race_name": race_id.title(),
            "playbook_id": race_id + "__1", "start_epoch": 1_000_000,
            "oracle": {"total_hours": 40.0, "favored_side": "right"},
            "regret": {"minutes": 18, "side_paid": "right", "recommended_side": "left",
                       "side_matched": False},
            "playbook": {}, "caveat": "",
            "actual_track": {"available": True, "source": "gpx", "elapsed_hours": 42.0,
                             "time_behind_optimal_min": 120, "extra_distance_pct": 8,
                             "xte_mean_nm": 2.1, "xte_p90_nm": 5.0, "xte_max_nm": 9.0,
                             "side_worked": "right", "polar_pct": polar_pct, "polar_samples": 300,
                             "perf_bins": bins},
            "critique": {"model": "deterministic", "assessment": "x"}}


def test_learning_flow():
    tmp = tempfile.mkdtemp()
    learning.LEARNING_DB = os.path.join(tmp, "learning.db")
    boat = {"boat_id": "_testboat_", "name": "Test", "draft_m": 2.0, "helm_factor": 1.0}
    boats.save_boat(boat)
    try:
        # archive two races; the boat is relatively WEAK upwind (88% of polar) vs reaching (98%)
        bins_a = [{"tws": 12.0, "twa": 45.0, "point_of_sail": "upwind", "samples": 40,
                   "best_stw": 6.2, "target_stw": 7.0, "pct": 88},
                  {"tws": 12.0, "twa": 90.0, "point_of_sail": "reaching", "samples": 40,
                   "best_stw": 8.3, "target_stw": 8.5, "pct": 98}]
        id1 = learning.archive_debrief(_fake_report("alpha", bins_a, 92), "_testboat_")
        id2 = learning.archive_debrief(_fake_report("beta", bins_a, 93), "_testboat_")
        assert id1 and id2 and id1 != id2
        dl = learning.list_debriefs("_testboat_")
        assert len(dl) == 2 and dl[0]["race_id"] in ("alpha", "beta"), dl
        full = learning.get_debrief(id1)
        assert full["perf_bins"] and full["report"]["race_id"] == "alpha", full

        # PROPOSE — must NOT change the boat (human gate)
        before = boats.get_boat("_testboat_")
        prop = learning.propose("_testboat_")
        assert prop["ok"] and prop["status"] == "proposed", prop
        assert boats.get_boat("_testboat_")["helm_factor"] == before["helm_factor"], "propose mutated boat!"
        assert not boats.get_boat("_testboat_").get("polar_adjustments"), "propose wrote adjustments!"
        assert prop["helm_proposed"] < 1.0, prop                 # boat underperforms → helm < 1
        upwind_adj = [a for a in prop["adjustments"] if a["twa"] == 45.0]
        assert upwind_adj and upwind_adj[0]["mult"] < 1.0, prop  # relatively weak upwind → mult < 1
        print(f"PASS propose: helm {prop['helm_current']}→{prop['helm_proposed']}, "
              f"{len(prop['adjustments'])} cell adjustments, boat UNCHANGED (human gate holds)")

        # APPROVE (human) — now it lands on the boat
        res = learning.apply_proposal(prop["id"], note="looks right")
        assert res["ok"], res
        nb = boats.get_boat("_testboat_")
        assert nb["helm_factor"] == prop["helm_proposed"], nb
        assert nb["polar_adjustments"] and any(a["twa"] == 45.0 for a in nb["polar_adjustments"]), nb
        assert learning.get_proposal(prop["id"])["status"] == "applied"
        # can't re-apply an applied proposal
        assert not learning.apply_proposal(prop["id"])["ok"]
        print(f"PASS apply: boat helm_factor={nb['helm_factor']}, "
              f"{len(nb['polar_adjustments'])} approved polar adjustments written")

        # REJECT a fresh proposal leaves the boat alone
        p2 = learning.propose("_testboat_")
        hf_before = boats.get_boat("_testboat_")["helm_factor"]
        assert learning.reject_proposal(p2["id"], "not enough data")["ok"]
        assert learning.get_proposal(p2["id"])["status"] == "rejected"
        assert boats.get_boat("_testboat_")["helm_factor"] == hf_before
        print("PASS reject: rejected proposal leaves the boat untouched")
    finally:
        # cleanup the throwaway boat file
        for d in (os.path.join(os.environ.get("INGESTED_DIR", "/srv/ingested"), "boats"),):
            f = os.path.join(d, "_testboat_.json")
            if os.path.exists(f):
                os.remove(f)


def test_helm_can_exceed_one():
    """A boat that OUTPERFORMS the cert (rated soft) should get a proposed helm_factor > 1.0."""
    tmp = tempfile.mkdtemp()
    learning.LEARNING_DB = os.path.join(tmp, "learning.db")
    boats.save_boat({"boat_id": "_softboat_", "name": "Soft", "draft_m": 2.0, "helm_factor": 1.0})
    try:
        bins = [{"tws": 12.0, "twa": 90.0, "point_of_sail": "reaching", "samples": 80,
                 "best_stw": 8.9, "target_stw": 8.05, "pct": 111}]   # 111% of polar (current-corrected)
        learning.archive_debrief(_fake_report("soft-a", bins, 111), "_softboat_")
        learning.archive_debrief(_fake_report("soft-b", bins, 110), "_softboat_")
        p = learning.propose("_softboat_")
        assert p["ok"] and p["helm_proposed"] > 1.0, p          # learns "faster than rated"
        assert p["helm_proposed"] <= 1.15, p                    # but clamped
        learning.apply_proposal(p["id"])
        assert boats.get_boat("_softboat_")["helm_factor"] > 1.0
        print(f"PASS helm>1: soft-rated boat → helm_proposed {p['helm_proposed']} (>1.0), applied")
    finally:
        f = os.path.join(os.environ.get("INGESTED_DIR", "/srv/ingested"), "boats", "_softboat_.json")
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    test_polar_overlay()
    test_learning_flow()
    test_helm_can_exceed_one()
    print("\nALL LEARNING TESTS PASSED")
