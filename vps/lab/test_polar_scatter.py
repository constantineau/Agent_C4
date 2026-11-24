"""Log points behind the observed polar (#376).

What must hold:
1. The scatter is the SAME evidence as the cells: trust-gated, TWA-floored, instruments only.
   A point the polar refused must never be drawn behind it.
2. Thinning is capped per (TWS bucket, race) and in total, keeps first/last and time order, and
   counts what it left out.
3. The build version moved, so an old cache without the scatter is a miss.
"""
import pytest  # noqa: F401  (CI runs a file under pytest only when it imports it)

from app import monitor
from app import obspolar as OP
from app import polarscatter as PS

A = 1784155457.9


def _agent(fixes, danger=None):
    def fake(path, timeout=None):
        if path.startswith("/racelog/sessions"):
            return {"sessions": [{"id": 1, "name": "Race A", "race_id": "r", "boat_id": "b1",
                                  "window": {"start_ts": A, "end_ts": A + 3600, "hours": 1.0}}]}
        if path.startswith("/racelog/track"):
            return {"fixes": fixes, "sail_log": [{"t": A - 10, "flying": ["J1"]}]}
        return {"danger": danger or {}, "summary": {"line": "ok", "danger_s": 0}}
    return fake


def test_scatter_is_the_gated_evidence(monkeypatch):
    fixes = ([{"t": A + i, "tws": 10.2, "twa": 271.0, "stw": 6.0 + i / 100} for i in range(100)]
             + [{"t": A + 100 + i, "tws": 10.0, "twa": 12.0, "stw": 5.5} for i in range(50)]    # motoring
             + [{"t": A + 200 + i, "tws": 10.0, "twa": 90.0, "stw": 9.9} for i in range(30)]    # danger
             + [{"t": A + 300 + i, "tws": None, "twa": 90.0, "stw": 7.0} for i in range(20)])   # no wind
    monkeypatch.setattr(monitor, "agent_json", _agent(fixes, {"heading": [[A + 200, A + 229]]}))
    art = OP.compute(min_seconds=30, boat_id="b1")
    sc = art["scatter"]
    assert sc["fields"] == list(PS.FIELDS)
    assert sc["total"] == 100 and sc["kept"] == 100
    rows = [dict(zip(sc["fields"], r)) for r in sc["rows"]]
    assert all(r["twa"] == 89.0 for r in rows)          # folded, raw (not bucketed)
    assert all(r["tws"] == 10.2 for r in rows)          # raw TWS, the band is the UI's call
    assert all(r["config"] == "J1" and r["recording"] == A for r in rows)
    assert not any(r["stw"] == 9.9 or r["stw"] == 5.5 for r in rows)


def test_thinning_caps_and_counts():
    pts = [{"t": float(i), "tws": 10.0, "twa": 90.0, "stw": 6.0, "recording": 1} for i in range(1000)]
    pts += [{"t": float(i), "tws": 16.0, "twa": 90.0, "stw": 7.0, "recording": 1} for i in range(40)]
    out = PS.pack(pts, 2.0, per_group=100, total=10000)
    assert out["total"] == 1040 and out["kept"] == 140
    ts = [r[0] for r in out["rows"] if r[1] == 10.0]
    assert ts[0] == 0.0 and ts[-1] == 999.0 and ts == sorted(ts)
    # the total cap scales every group, each keeps at least one point
    out = PS.pack(pts, 2.0, per_group=100, total=50)
    assert out["kept"] <= 50 and any(r[1] == 16.0 for r in out["rows"])


def test_rows_carry_the_cells_own_bins(monkeypatch):
    # 9.0 kn and 8.96 kn are the 8 kn cell in this build (round half to even); the row says so,
    # so the page can't re-derive it differently. A cell short of the gate keeps its points here
    # (the page hides them per view), but they carry the bins to look the cell up.
    fixes = [{"t": A + i, "tws": 9.0 if i % 2 else 8.96, "twa": 85.0, "stw": 6.0} for i in range(60)]
    monkeypatch.setattr(monitor, "agent_json", _agent(fixes))
    art = OP.compute(min_seconds=30, boat_id="b1")
    rows = [dict(zip(art["scatter"]["fields"], r)) for r in art["scatter"]["rows"]]
    assert rows and all(r["band"] == 8.0 and r["twa_bin"] == 80.0 for r in rows)
    assert {(c["tws"], c["twa"]) for c in art["cells"]} == {(8.0, 80.0)}


def test_overlapping_windows_count_a_second_once_in_the_pooled_view(monkeypatch):
    # two sessions whose windows overlap by 50 s: each race's own view keeps the overlap, the
    # pooled polar counts it once, and the points say which copy is the pooled one
    B = A + 50

    def fake(path, timeout=None):
        if path.startswith("/racelog/sessions"):
            return {"sessions": [
                {"id": 1, "name": "A", "race_id": "r", "boat_id": "b1",
                 "window": {"start_ts": A, "end_ts": A + 100, "hours": 0.03}},
                {"id": 2, "name": "B", "race_id": "r", "boat_id": "b1",
                 "window": {"start_ts": B, "end_ts": B + 100, "hours": 0.03}}]}
        if path.startswith("/racelog/track"):
            st = float(path.split("start=")[1].split("&")[0])
            return {"fixes": [{"t": st + i, "tws": 10.0, "twa": 90.0, "stw": 6.0} for i in range(100)],
                    "sail_log": []}
        return {"danger": {}, "summary": {"line": "ok", "danger_s": 0}}
    monkeypatch.setattr(monitor, "agent_json", fake)
    art = OP.compute(min_seconds=30, boat_id="b1")
    rows = [dict(zip(art["scatter"]["fields"], r)) for r in art["scatter"]["rows"]]
    assert len(rows) == 200
    pooled = [r for r in rows if r["pooled"]]
    assert len(pooled) == 150 == art["cells"][0]["samples"]
    assert len({r["t"] for r in pooled}) == 150
    assert all(r["race"] for r in rows)        # under the cap, both samples keep everything


def test_short_unique_stretch_keeps_its_pooled_dots():
    # race 2 is 1000 s, all but 5 s of it already counted under race 1. Thinned as one list to 50,
    # its 5 pooled seconds (mid-race) would fall between the picks; as their own sample they stay.
    pts = [{"t": float(i), "tws": 10.0, "twa": 90.0, "stw": 6.0, "recording": 2,
            "band": 10.0, "twa_bin": 90.0, "pooled": int(500 <= i < 505)} for i in range(1000)]
    out = PS.pack(pts, 2.0, per_group=50, total=6000)
    rows = [dict(zip(out["fields"], r)) for r in out["rows"]]
    assert sorted(r["t"] for r in rows if r["pooled"]) == [500.0, 501.0, 502.0, 503.0, 504.0]
    assert sum(r["race"] for r in rows) == 50 == out["kept"]
    assert out["kept_pooled"] == 5 and out["total_pooled"] == 5
    # each sample keeps to the total cap on its own
    out = PS.pack(pts, 2.0, per_group=1000, total=20)
    rows = [dict(zip(out["fields"], r)) for r in out["rows"]]
    assert sum(r["race"] for r in rows) == 20 and sum(r["pooled"] for r in rows) == 5


def test_total_cap_holds_with_more_groups_than_points():
    pts = [{"t": 0.0, "tws": 10.0, "twa": 90.0, "stw": 6.0, "recording": i} for i in range(6001)]
    out = PS.pack(pts, 2.0)
    assert out["kept"] == PS.TOTAL and out["total"] == 6001
    pts = [{"t": float(j), "tws": 10.0, "twa": 90.0, "stw": 6.0, "recording": i}
           for i in range(7) for j in range(3)]
    assert PS.pack(pts, 2.0, total=10)["kept"] == 10


def test_old_cache_is_a_miss():
    assert OP.BUILD_VERSION >= 4
