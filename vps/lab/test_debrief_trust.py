"""The trust gate — danger windows never reach the learning loop, and the refusal is visible.

Item A's rule, per Cole 2026-09-09: **refuse** bins from windows the cross-check called
`danger`, per-channel, and print what was refused. Not down-weight (a (TWS,TWA) coordinate
derived from a broken compass files data in the wrong bin — wrong at any weight), and never
silently (a check nobody can see is worth what a check never written is worth).

These tests drive `track.score_track` with a synthetic track and a trust blob shaped exactly
like the agent's `/racelog/trust` response. What matters, in order: samples inside a danger
interval are refused from the polar/bin inputs; `unknown` is honest silence and is NOT refused;
no trust at all refuses nothing but says the track is unguarded; the count and the channel names
are in the output where the debrief (and `archive_debrief`) can see them.

Run in-container (no external deps needed beyond the lab image):
  docker run --rm -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \
    -v $PWD/vps/lab/test_debrief_trust.py:/srv/test_debrief_trust.py:ro \
    -w /srv sr33-dev-lab python test_debrief_trust.py
"""
from app import track as T

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784394000.0
N = 120


def mktrack(trust=None):
    """A two-hour straight-line sail at 6 kn, one fix a minute, absolute clock."""
    return {"source": "boatlog",
            "fixes": [{"t": T0 + i * 60, "lat": 45.0 + i * 0.0015, "lon": -83.0,
                       "sog": 6.0, "cog": 0.0} for i in range(N)],
            "trust": trust, "sail_log": []}


ORACLE = {"path": [{"lat": 45.0, "lon": -83.0}, {"lat": 45.0 + (N - 1) * 0.0015, "lon": -83.0}],
          "total_hours": 2.0, "total_nm": 10.8}
MARKS = [("Start", "start", 45.0, -83.0), ("Finish", "finish", 45.0 + (N - 1) * 0.0015, -83.0)]


def score(trust):
    return T.score_track(mktrack(trust), ORACLE, MARKS, T0)


print("the gate:")
DANGER = {"available": True,
          "channels": {"heading": [
              {"t0": T0 + 60 * 60, "t1": T0 + 80 * 60, "status": "danger",
               "reason": "heading reads -98° off GPS course"}]},
          "danger": {"heading": [(T0 + 60 * 60, T0 + 80 * 60)], "attitude": []},
          "summary": {"danger_s": 1200, "warn_s": 0, "unknown_s": 0,
                      "line": "heading DANGER for 20 min — -98° off GPS course"}}
r = score(DANGER)
check("samples inside the danger window are refused (21 of 120 fixes fall in 60–80 min)",
      r["trust"]["refused_samples"] == 21)
check("...and the refusing channel is named", r["trust"]["refused_channels"] == ["heading"])
check("...and the human line travels with the score",
      "DANGER" in (r["trust"]["line"] or ""))

r = score({"available": True, "channels": {}, "danger": {"heading": [], "attitude": []},
           "summary": {"danger_s": 0, "warn_s": 0, "unknown_s": 0, "line": "no danger windows"}})
check("a clean sweep (the Jul 15 shape) refuses nothing",
      r["trust"]["refused_samples"] == 0 and r["trust"]["available"] is True)

# unknown is honest silence: only DANGER intervals arrive in trust["danger"], so a sweep full of
# unknown segments refuses nothing — asserted via a trust blob whose segments are unknown but
# whose danger map is empty, which is exactly what trust_window.danger_intervals produces.
r = score({"available": True,
           "channels": {"heading": [{"t0": T0, "t1": T0 + N * 60, "status": "unknown",
                                     "reason": "samples disagree"}]},
           "danger": {"heading": [], "attitude": []},
           "summary": {"danger_s": 0, "warn_s": 0, "unknown_s": N * 60,
                       "line": "no danger windows"}})
check("unknown segments are never refused — honest silence is not corruption",
      r["trust"]["refused_samples"] == 0)

r = score(None)
check("no trust at all refuses nothing…", r["trust"]["refused_samples"] == 0)
check("…but the output SAYS the track is unguarded (available: False)",
      r["trust"]["available"] is False)

r = score({"available": False, "note": "trust sweep unavailable (agent unreachable) — NO bins "
                                       "were refused; treat refinements from this track as unguarded"})
check("a failed sweep is reported in the line, not swallowed",
      "unguarded" in (r["trust"]["line"] or ""))

print("\nmulti-channel:")
r = score({"available": True, "channels": {},
           "danger": {"heading": [(T0 + 10 * 60, T0 + 20 * 60)],
                      "attitude": [(T0 + 15 * 60, T0 + 30 * 60)]},
           "summary": {"danger_s": 1200, "warn_s": 0, "unknown_s": 0,
                       "line": "heading DANGER 10 min; attitude DANGER 15 min"}})
check("overlapping danger windows from two channels refuse the union (10–30 min = 21 fixes)",
      r["trust"]["refused_samples"] == 21)
check("...and both channels are named",
      r["trust"]["refused_channels"] == ["attitude", "heading"])

print("\nthe metrics that must NOT be gated:")
r_full = score(None)
r_gated = score(DANGER)
check("track-shape metrics (distance sailed) are identical gated and ungated — XTE and "
      "side-worked read GPS geometry, not the compass",
      r_full.get("dist_nm") == r_gated.get("dist_nm"))

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
