"""A blank Gameplan Start routes from the race start, not from now (issue #92).

Pure + offline: exercises app/racestart.resolve (the one place the default is decided) and the
optimize route's wiring, which must hand the resolved start to the router and echo why.
"""
from app import racestart

NOW = 1_780_000_000.0                       # a fixed "now" (2026-05-28) — races below are after it
GUN_E = NOW + 3 * 86400                     # our class (E) — three days out
GUN_H = NOW + 3 * 86400 - 1800              # an earlier class


def _race(**kw):
    d = {"race_id": "t", "timezone": "America/Detroit", "own": {"division": "Class E"},
         "division_starts": {"Class H": GUN_H, "Class E": GUN_E}}
    d.update(kw)
    return d


def test_blank_uses_our_division_gun():
    r = racestart.resolve(_race(), None, now=NOW)
    assert r["source"] == "race_start" and r["epoch"] == GUN_E, r
    assert r["race_start"]["division"] == "Class E" and "Class E gun" in r["note"], r
    print("PASS blank Start → our division's gun")


def test_no_own_gun_uses_earliest():
    r = racestart.resolve(_race(own={"division": "Class Z"}), None, now=NOW)
    assert r["source"] == "race_start" and r["epoch"] == GUN_H, r
    assert r["race_start"]["basis"] == "earliest_gun", r
    print("PASS no gun for our class → earliest gun")


def test_iso_local_gun_from_si_extraction():
    # the SI extraction stores ISO local strings; 2026-07-18 13:30 EDT = 17:30 UTC
    r = racestart.resolve(_race(division_starts={"Class E": "2026-07-18T13:30:00"}), None, now=NOW)
    assert r["source"] == "race_start" and r["epoch"] == 1784395800.0, r
    print("PASS ISO race-local gun interpreted in the venue timezone")


def test_picked_start_wins():
    r = racestart.resolve(_race(), NOW + 3600, now=NOW)
    assert r["source"] == "picked" and r["epoch"] == NOW + 3600, r
    print("PASS a picked Start is used as-is")


def test_no_gun_routes_from_now_and_says_so():
    r = racestart.resolve(_race(division_starts={}), None, now=NOW)
    assert r["source"] == "now_no_gun" and r["epoch"] == NOW and "No gun time" in r["note"], r
    r = racestart.resolve({"race_id": "t", "division_starts": {"Class E": None}}, "", now=NOW)
    assert r["source"] == "now_no_gun", r
    print("PASS no gun time → now, with a plain reason")


def test_passed_start_routes_from_now_and_says_so():
    r = racestart.resolve(_race(), None, now=GUN_E + 60)
    assert r["source"] == "now_passed" and r["epoch"] == GUN_E + 60, r
    assert "has passed" in r["note"], r
    print("PASS start already gone → now, with a plain reason")


def test_optimize_route_passes_race_start():
    """The /api/optimize handler must route from the gun when Start is blank, and echo why."""
    import asyncio
    from app import main, store, jobs
    race = _race(division_starts={"Class E": 4_000_000_000})       # far future → stays race_start
    seen = {}
    orig_get, orig_run, orig_start = store.get_race, main._run_optimize, jobs.start
    try:
        store.get_race = lambda rid: race
        main._run_optimize = lambda d, c, se, *a, **k: seen.setdefault("se", se) and {"available": False}
        jobs.start = lambda name, work, meta=None: work(lambda m: None)
        out = asyncio.run(main.optimize({"race_id": "t"}))
        assert seen["se"] == 4_000_000_000, seen
        assert out["start"]["source"] == "race_start", out
        d, _c, se, _m, _e = main._playbook_params({"race_id": "t"})
        assert se == 4_000_000_000, se
        assert main.race_start_default("t")["epoch"] == 4_000_000_000
    finally:
        store.get_race, main._run_optimize, jobs.start = orig_get, orig_run, orig_start
    print("PASS optimize + playbook routes default to the race start")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
