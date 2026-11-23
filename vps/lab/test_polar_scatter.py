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


def test_old_cache_is_a_miss():
    assert OP.BUILD_VERSION >= 4
