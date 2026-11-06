"""#52 pace plays: the play names the mark (not its sequence number), and "same sails" compares the
re-route against the sails the nominal actually flies after that mark (its timed sail plan, so a
mid-leg peel counts), not one headline sail per leg.

Drives playbook._internal_fan with the optimizer stubbed out (no wind, no routing, no network).
"""
from app import optimizer as OPT       # noqa: E402
from app import playbook as PB         # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_750_000_000
MARKS = [(0, "Start", 45.0, -82.0), (7, "Cove Island", 45.3, -82.0), (9, "Finish", 45.6, -82.0)]

# Nominal: leg 1 on J1 (headline J1), leg 2 peels J1 -> A3 mid-leg; the per-leg headline of leg 2
# is "J1" (its first stretch), so the old headline compare saw only ["J1"] after the mark.
NOMINAL = {
    "total_hours": 4.0,
    "legs": [{"to": "Cove Island", "leg_minutes": 120.0, "eta_epoch": T0 + 7200, "sail": "J1"},
             {"to": "Finish", "leg_minutes": 120.0, "eta_epoch": T0 + 14400, "sail": "J1"}],
    "path": [{"lat": 45.0, "lon": -82.0, "t": T0},
             {"lat": 45.3, "lon": -82.0, "t": T0 + 7200},        # mark (leg_end_idx 1)
             {"lat": 45.4, "lon": -82.0, "t": T0 + 9000},
             {"lat": 45.6, "lon": -82.0, "t": T0 + 14400}],
    "leg_end_idx": [1, 3],
    "sail_plan": [{"sail": "J1", "t": T0 + 3600}, {"sail": "A3", "t": T0 + 14400}],
    "total_tacks": 0, "total_peels": 1,
}


def run_fan(reroute):
    """Run _internal_fan with every pace re-route returning `reroute` (and nothing else routed)."""
    saved = (OPT.optimize_course, OPT.incomplete_reason, PB._favored_side, PB._mean_xte_nm,
             PB._rejoin_tab)
    OPT.optimize_course = lambda *a, **kw: dict(reroute)
    OPT.incomplete_reason = lambda res, fin: None
    PB._favored_side = lambda *a: "middle"
    PB._mean_xte_nm = lambda path, ref: 0.5              # on the nominal line
    PB._rejoin_tab = lambda *a, **kw: []
    try:
        return PB._internal_fan({}, "c", T0, None, NOMINAL, None, None, MARKS, per_budget_s=1)
    finally:
        (OPT.optimize_course, OPT.incomplete_reason, PB._favored_side, PB._mean_xte_nm,
         PB._rejoin_tab) = saved


def pace(rows):
    return [r for r in rows if str(r.get("id", r.get("scenario", ""))).startswith("pace_")]


print("1) pace play names the mark, not its sequence number")
same = {"total_hours": 2.0, "path": [], "sail_plan": [{"sail": "J1", "t": T0}, {"sail": "A3", "t": T0 + 5000}]}
routes, robust = run_fan(same)
names = [r["name"] for r in pace(routes) + pace(robust)]
check("every pace entry exists (3 delays at the one intermediate mark)", len(names) == 3)
check("names read 'at Cove Island'", names and all(n.endswith("at Cove Island") for n in names))
check("no name reads 'at 7' (the mark's seq)", not any(n.endswith(" at 7") for n in names))

print("2) same sails = the sails actually flown after the mark (peel counts)")
check("nominal after the mark is J1 then A3 (the mid-leg peel)",
      PB._sail_seq_from(NOMINAL, 1) == ["J1", "A3"])
check("re-route flying J1 -> A3 HOLDS (robustness, not a play)",
      len(pace(robust)) == 3 and not pace(routes))
check("robustness note says the plan holds", pace(robust) and all("HOLDS" in r["note"] for r in pace(robust)))

no_peel = {"total_hours": 2.0, "path": [], "sail_plan": [{"sail": "J1", "t": T0}]}
routes, robust = run_fan(no_peel)
check("re-route that never peels to the A3 DIFFERS (published as plays)",
      len(pace(routes)) == 3 and not pace(robust))

print("3) the sail in force on the segment leaving the mark, not the inbound one")
peel_at_mark = dict(NOMINAL, sail_plan=[{"sail": "J1", "t": T0 + 3600},
                                         {"sail": "A3", "t": T0 + 9000}])   # first segment after the mark
check("a peel on the first segment after the mark starts the remainder on the A3",
      PB._sail_seq_from(peel_at_mark, 1) == ["A3"])

print("4) no timed sail plan (sail-aware off) falls back to the per-leg headline sails")
untimed = dict(NOMINAL, sail_plan=[{"sail": "J1", "from_leg": "Cove Island", "to_leg": "Finish"}])
check("fallback reads legs[k:] headline sails", PB._sail_seq_from(untimed, 1) == ["J1"])

print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
