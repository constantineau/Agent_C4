"""Debrief actual-track ingestion — GPX parse, YB AllPositions3 decode, helm-vs-optimal scoring.

Deterministic + offline: builds a synthetic AllPositions3 binary in the exact reverse-engineered
format and asserts the decode round-trips; parses a tiny GPX; and scores a known track vs a known
oracle line. Run in-container:  docker compose -f compose.dev.yml exec -w /srv lab python test_routing_track.py
(or `docker cp` then exec). A separate LIVE check (network) decodes a real YB race — see __main__.
"""
import struct
import sys

from app import track


def _ap3(base_lat, base_lon, deltas):
    """Build a 1-team AllPositions3 buffer. `deltas` are (dt, dlat_e5, dlon_e5) newest->oldest."""
    b = bytes.fromhex("0266851e") + b"\x00\x00\x00\x00\x00"      # magic + 5-byte global header
    b += struct.pack(">iiI", round(base_lat * 1e5), round(base_lon * 1e5), 0)
    for dt, dla, dlo in deltas:
        b += struct.pack(">HhhH", 0x8000 | dt, dla, dlo, 0)
    return b


def test_yb_decode():
    # base (newest) = 45.0,-84.0 ; two 300 s steps of -100e-5 lat / +50e-5 lon walking back in time
    b = _ap3(45.0, -84.0, [(300, -100, 50), (300, -100, 50)])
    blocks = track._decode_allpositions3(b, 1)
    assert len(blocks) == 1, blocks
    fx = blocks[0]
    assert len(fx) == 3, fx
    # chronological: oldest first (start), newest last (base)
    assert abs(fx[0]["lat"] - 44.998) < 1e-6 and abs(fx[0]["lon"] - (-83.999)) < 1e-6, fx[0]
    assert abs(fx[-1]["lat"] - 45.0) < 1e-6 and abs(fx[-1]["lon"] - (-84.0)) < 1e-6, fx[-1]
    assert fx[0]["t"] == 0 and fx[-1]["t"] == 600, [f["t"] for f in fx]
    print("PASS yb_decode: 3 fixes, chronological, t 0->600")


def test_yb_decode_two_teams():
    # team0 then an inter-team header (high-bit clear u16) then team1 base
    b0 = _ap3(45.0, -84.0, [(300, -100, 50)])
    hdr = struct.pack(">HHI", 0x0002, 0x0000, 0)                  # 8-byte inter-team header (high bit clear)
    b1 = struct.pack(">iiI", round(43.0 * 1e5), round(-82.0 * 1e5), 0) + struct.pack(">HhhH", 0x8000 | 60, 200, -100, 0)
    blocks = track._decode_allpositions3(b0 + hdr + b1, 2)
    assert len(blocks) == 2, len(blocks)
    assert abs(blocks[1][-1]["lat"] - 43.0) < 1e-6, blocks[1][-1]
    print("PASS yb_decode_two_teams: resync found both blocks")


def test_gpx():
    gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>'
           '<trkpt lat="43.00" lon="-82.40"><time>2026-07-12T16:00:00Z</time></trkpt>'
           '<trkpt lat="43.01" lon="-82.30"><time>2026-07-12T16:30:00Z</time></trkpt>'
           '<trkpt lat="43.00" lon="-82.00"><time>2026-07-12T17:00:00Z</time></trkpt>'
           '</trkseg></trk></gpx>')
    t = track.parse_gpx(gpx.encode())
    assert t["n"] == 3 and t["source"] == "gpx", t
    assert t["fixes"][1]["sog"] and t["fixes"][1]["cog"] is not None, t["fixes"][1]
    print("PASS gpx: 3 trkpts parsed with derived sog/cog")


def test_score():
    # due-east leg; oracle = the rhumb; boat bulges north (oversail + XTE) and takes a bit longer
    marks = [("Start", "start", 43.0, -82.4), ("Finish", "finish", 43.0, -82.0)]
    oracle = {"path": [{"lat": 43.0, "lon": -82.4, "t": 0}, {"lat": 43.0, "lon": -82.0, "t": 3600}],
              "total_hours": 1.0, "total_sailed_nm": track._hav_nm((43.0, -82.4), (43.0, -82.0))}
    fixes = [{"lat": 43.0, "lon": -82.4, "t": 0, "sog": 6, "cog": 90},
             {"lat": 43.04, "lon": -82.2, "t": 2400, "sog": 6, "cog": 90},
             {"lat": 43.0, "lon": -82.0, "t": 4500, "sog": 6, "cog": 90}]
    trk = {"source": "gpx", "fixes": fixes}
    s = track.score_track(trk, oracle, marks, 1_000_000)
    assert s["available"], s
    assert s["extra_distance_pct"] > 0, s            # bulge => sailed further than the rhumb
    assert s["xte_max_nm"] > 1.0, s                  # ~0.04 deg north ≈ 2.4 nm off the line
    assert s["time_behind_optimal_min"] == 15, s     # 1.25 h vs 1.0 h
    assert s["side_worked"] in ("left", "right", "middle"), s
    assert s.get("polar_pct") is None, "no windfield => no polar%"
    print(f"PASS score: +{s['extra_distance_pct']}% oversail, xte_max {s['xte_max_nm']} nm, "
          f"{s['time_behind_optimal_min']} min behind, side={s['side_worked']}")

    # boat much FASTER than the oracle => non-physical => a caveat (oracle-window mismatch)
    fast = [{"lat": 43.0, "lon": -82.4, "t": 0, "sog": 8, "cog": 90},
            {"lat": 43.0, "lon": -82.0, "t": 600, "sog": 8, "cog": 90}]
    s2 = track.score_track({"source": "yb", "fixes": fast}, oracle, marks, 1_000_000)
    assert s2["time_behind_optimal_min"] < -20 and s2.get("caveats"), s2
    print(f"PASS caveat: {s2['time_behind_optimal_min']} min => {s2['caveats'][0][:48]}…")


def test_current_correction():
    # boat makes 7 kn THROUGH THE WATER due east; a 2 kn east-setting current → 9 kn SOG east.
    stw, course = track._water_velocity(9.0, 90.0, 90.0, 2.0)     # (sog, cog, set, drift)
    assert abs(stw - 7.0) < 1e-6 and abs(course - 90.0) < 1e-3, (stw, course)
    # a 2 kn FOUL current (sets west, 270) with only 5 kn SOG east → 7 kn through the water.
    stw2, _ = track._water_velocity(5.0, 90.0, 270.0, 2.0)
    assert abs(stw2 - 7.0) < 1e-6, stw2
    print("PASS current_correction: SOG 9→STW 7 (fair tide removed), SOG 5→STW 7 (foul tide added back)")


def test_perf_bins_snap_to_cert():
    # a fake wind field (constant 12 kn from north) + a cert with specific TWA cells; observations at
    # ~50° and ~135° must SNAP to the cert's 52° and 135° cells (so an overlay later lines up 1:1).
    class WF:
        loaded = True
        def wind_at(self, lat, lon, ep):
            return (12.0, 0.0)            # 12 kn TWS, TWD=0 (north)
    cert = [(12.0, 52.0, 7.42), (12.0, 90.0, 8.05), (12.0, 135.0, 7.72)]
    # cog 50 → twa 50 (snaps to 52); cog 134 → twa 134 (snaps to 135)
    seg = ([{"lat": 43.0, "lon": -82.0, "t": i, "sog": 7.0, "cog": 50} for i in range(6)] +
           [{"lat": 43.0, "lon": -82.0, "t": 100 + i, "sog": 7.5, "cog": 134} for i in range(6)])
    epochs = [1_000_000 + f["t"] for f in seg]
    bins = track._performance_bins(seg, epochs, WF(), cert)
    cells = {(b["tws"], b["twa"]) for b in bins}
    assert (12.0, 52.0) in cells and (12.0, 135.0) in cells, bins
    assert all(b["target_stw"] in (7.42, 7.72, 8.05) for b in bins), bins
    print(f"PASS perf_bins snap: observations at 50°/134° snapped to cert cells {sorted(cells)}")


def test_wave_correction():
    # constant 12 kn N wind + a 2 m head sea; boat sails 6.9 kn upwind (twa ~50) vs a 7.42 cert target.
    # RAW polar% is depressed by the seaway; helm_pct divides the wave loss back out (flat-water helm).
    class WF:
        loaded = True
        def wind_at(self, lat, lon, ep):
            return (12.0, 0.0)
    class Waves:
        loaded = True
        def wave_at(self, lat, lon, ep):
            return 2.0
    cert = [(12.0, 52.0, 7.42)]
    seg = [{"lat": 43.0, "lon": -82.0, "t": i, "sog": 6.9, "cog": 50} for i in range(8)]
    epochs = [1_000_000 + f["t"] for f in seg]
    flat = track._polar_pct(seg, epochs, WF(), cert)                      # no wave field
    wav = track._polar_pct(seg, epochs, WF(), cert, None, Waves())        # wave-corrected
    assert flat["polar_pct"] == wav["polar_pct"], (flat, wav)            # raw vs-flat-polar unchanged
    assert not flat.get("wave_corrected") and wav["wave_corrected"], (flat, wav)
    assert wav["helm_pct"] > wav["polar_pct"], wav                       # sea state excused
    assert wav["sea_state_hs_mean"] == 2.0, wav
    bins = track._performance_bins(seg, epochs, WF(), cert, None, Waves())
    b = bins[0]
    assert b["hs_mean"] == 2.0 and b["pct_flat"] > b["pct"], b           # cell carries Hs + flat pct
    print(f"PASS wave_correction: polar {wav['polar_pct']}% raw -> helm {wav['helm_pct']}% flat-water "
          f"(2 m head sea); bin pct {b['pct']}->pct_flat {b['pct_flat']}")


def _live():
    """Network: decode a real YB race and validate against its GetPositions latest fix."""
    import json, urllib.request
    race = "bayviewmack2025"
    g = lambda u, raw=False: (lambda d: d if raw else json.loads(d.decode("utf-8", "replace")))(
        urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "M/5"}), timeout=60).read())
    teams = g(f"https://cf.yb.tl/JSON/{race}/RaceSetup")["teams"]
    b = g(f"https://cf.yb.tl/BIN/{race}/AllPositions3", raw=True)
    blocks = track._decode_allpositions3(b, len(teams))
    sane = sum(1 for p in blocks if len(p) > 2 and
               max(x["lat"] for x in p) - min(x["lat"] for x in p) < 25)
    idx = next(i for i, t in enumerate(teams) if t["name"] == "Illuminati")
    fx = blocks[idx]
    print(f"LIVE {race}: {len(blocks)} blocks, {sane} sane; Illuminati {len(fx)} fixes "
          f"start=({fx[0]['lat']:.3f},{fx[0]['lon']:.3f}) finish=({fx[-1]['lat']:.3f},{fx[-1]['lon']:.3f})")
    assert 43.0 < fx[0]["lat"] < 43.2 and 45.7 < fx[-1]["lat"] < 46.0, "expected Port Huron -> Mackinac"
    print("PASS live: real YB track decodes Port Huron -> Mackinac")


if __name__ == "__main__":
    test_yb_decode(); test_yb_decode_two_teams(); test_gpx(); test_score()
    test_current_correction(); test_perf_bins_snap_to_cert(); test_wave_correction()
    if "--live" in sys.argv:
        _live()
    print("\nALL TRACK TESTS PASSED")


def test_config_attribution():
    """Per-config polar development: the sails-bar log attributes each fix to its configuration."""
    from app import track as T
    log = [{"ts": 100, "flying": ["J1"], "reef": None},
           {"ts": 200, "flying": ["C0", "J2"], "reef": None},
           {"ts": 300, "flying": ["A3", "SS"], "reef": "R1"}]
    assert T.config_at(log, 50) is None            # before the first entry
    assert T.config_at(log, 150) == "J1"
    assert T.config_at(log, 250) == "C0+J2"        # a combination the crossover chart doesn't rate
    assert T.config_at(log, 999) == "A3+SS+R1"     # kite + staysail + reef
    assert T.config_at([], 150) is None            # no log (GPX/YB tracks)
    assert T.config_at(log, None) is None
    # doused everything -> None again
    log2 = log + [{"ts": 400, "flying": [], "reef": None}]
    assert T.config_at(log2, 450) is None
    # save/load round-trips the sail log with the track
    import tempfile, os
    T.TRACK_DIR = tempfile.mkdtemp()
    meta = T.save_track("cfg-test", {"source": "boatlog", "fixes": [{"t": 1, "lat": 0, "lon": 0}],
                                     "sail_log": log})
    assert meta["sail_changes"] == 3
    back = T.load_track("cfg-test")
    assert len(back["sail_log"]) == 3 and back["sail_log"][1]["flying"] == ["C0", "J2"]
    print("  [OK ] config attribution + sail-log round-trip")


test_config_attribution()
