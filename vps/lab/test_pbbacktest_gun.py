"""pbbacktest.sample_replay with staggered starts (#341): the output `gun` is the gun asked for,
and each boat keeps its own start as `division_gun`. Before the fix the per-boat loop overwrote
`gun`, so the output recorded the last sampled boat's start.

No network: the retro archive, the race store and the oracle wind field are stubbed.
Run in the Lab image: python3 -m pytest -q vps/lab/test_pbbacktest_gun.py
"""
import json

import pytest  # noqa: F401 -- tools/ci/run.sh runs files that import pytest under pytest

from app import pbbacktest as PB

GUN = 1_800_000_000
STARTS = {1: GUN, 2: GUN + 600, 3: GUN + 1200}     # staggered division starts


class _Wind:
    loaded = True

    def wind_at(self, lat, lon, t):
        return (10.0, 220.0)


def test_output_gun_is_the_race_gun(tmp_path, monkeypatch):
    monkeypatch.setattr(PB, "OUT_DIR", str(tmp_path))
    monkeypatch.setattr(PB.store, "get_race", lambda rid: {"race_id": rid})
    monkeypatch.setattr(PB.optimizer, "course_bbox", lambda d, c: (46.0, 45.0, -84.0, -83.0))
    monkeypatch.setattr(PB.retrostore, "get_entries", lambda rid: [
        {"team_id": t, "boat": f"B{t}", "start_epoch": s} for t, s in STARTS.items()])
    monkeypatch.setattr(PB.retrostore, "get_results", lambda rid: [])
    monkeypatch.setattr(PB.retrostore, "get_track", lambda rid, tid: [
        {"t": STARTS[tid] + i * 900, "lat": 45.5, "lon": -83.5, "cog": 0, "sog": 6}
        for i in range(4)])
    monkeypatch.setattr(PB, "build_oracle_field", lambda *a, **k: _Wind())
    r = PB.sample_replay({}, team_ids=[1, 2, 3], retro_race_id="r", def_race_id="d",
                         course_id="c", gun=GUN, step_s=900, hours_cap=1.0)
    assert r["ok"], r
    out = json.load(open(tmp_path / "replay_input.json"))
    assert out["gun"] == GUN, out["gun"]
    assert {k: v["division_gun"] for k, v in out["boats"].items()} == {
        str(t): s for t, s in STARTS.items()}
