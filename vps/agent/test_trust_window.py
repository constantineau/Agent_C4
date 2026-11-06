"""Debrief trust over a recorded window (app/trust_window.py): the guardrail that keeps a broken
compass out of the boat model.

Drives the pure sweeps with synthetic samples shaped like Jul 18 (a healthy stretch, then the
heading ~90° off GPS course, then a one-sample attitude kick) and checks the segments, the danger
intervals the debrief refuses, the summary line, the publisher pick and the heading/COG pairing.
No DB or network.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_trust_window.py
"""
from app import sensor_health, trust_window as TW

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1_752_876_000.0      # 2025-07-18 22:00Z, any fixed epoch will do
COG = 330.0


def heading_track(minutes_ok, minutes_bad, bias=90.0, every_s=10):
    """[(t, hdg, cog, sog)]: steady on COG 330 at 7 kn; the compass reads true, then `bias` off."""
    out = []
    for i in range(int((minutes_ok + minutes_bad) * 60 / every_s) + 1):
        t = T0 + i * every_s
        off = 0.0 if t < T0 + minutes_ok * 60 else bias
        out.append((t, (COG + off) % 360, COG, 7.0))
    return out


print("heading_segments:")
check("no samples → no segments", TW.heading_segments([]) == [])
segs = TW.heading_segments(heading_track(30, 60), step_s=60)
for s in segs:
    print(f"      {(s['t0'] - T0) / 60:5.0f}-{(s['t1'] - T0) / 60:3.0f} min  {s['status']:<7} {s['reason']}")
statuses = [s["status"] for s in segs]
check("starts ok", statuses[0] == "ok")
check("ends in danger, steady +90°", statuses[-1] == "danger" and abs(segs[-1]["bias_deg"] - 90) < 1)
check("consecutive same-status steps are merged", all(a != b for a, b in zip(statuses, statuses[1:])))
check("segments are contiguous and time-ordered",
      all(a["t1"] == b["t0"] for a, b in zip(segs, segs[1:])) and all(s["t0"] < s["t1"] for s in segs))
check("the first verdict waits one check window",
      segs[0]["t1"] - T0 >= sensor_health.HEADING_WINDOW_MIN * 60)
danger = [s for s in segs if s["status"] == "danger"]
check("the danger starts after the fault does", danger and danger[0]["t0"] >= T0 + 30 * 60)
check("a healthy race has no danger at all",
      all(s["status"] == "ok" for s in TW.heading_segments(heading_track(60, 0), step_s=60)))

slow = [(t, h, c, 1.0) for (t, h, c, _s) in heading_track(30, 0)]
check("too slow for COG → unknown, never danger",
      {s["status"] for s in TW.heading_segments(slow, step_s=60)} == {"unknown"})

print("attitude_segments:")
check("no samples → no segments", TW.attitude_segments([]) == [])
att = [(T0 + i, 10.0, 2.0) for i in range(0, 600)]
att[330] = (T0 + 330, 133.1, 2.0)                       # the Jul 18 kick: one impossible sample
asegs = TW.attitude_segments(att, step_s=60)
check(f"one impossible sample → a danger step ({[s['status'] for s in asegs]})",
      [s["status"] for s in asegs] == ["ok", "danger", "ok"])
check("…which names the roll", "roll +133.1°" in asegs[1]["reason"])
gap = [(T0, 10.0, 2.0), (T0 + 300, 10.0, 2.0)]
check("a step with no samples is unknown", "unknown" in [s["status"] for s in TW.attitude_segments(gap, 60)])

print("danger_intervals + summarize:")
check("only danger segments are refused", TW.danger_intervals(asegs) == [(asegs[1]["t0"], asegs[1]["t1"])])
check("nothing / None → nothing", TW.danger_intervals([]) == [] and TW.danger_intervals(None) == [])
summ = TW.summarize({"heading": segs, "attitude": asegs})
print(f"      {summ['line']}")
check("summary names both channels in DANGER",
      "heading DANGER for" in summ["line"] and "attitude DANGER for 1 min" in summ["line"])
check("danger seconds add up", summ["danger_s"] == round(sum(s["t1"] - s["t0"] for s in danger) + 60))
check("a clean window says so", TW.summarize({"heading": TW.heading_segments(heading_track(60, 0), 60)})
      == {"danger_s": 0, "warn_s": 0, "unknown_s": 0, "unchecked": [], "line": "no danger windows"})

print("pick_source:")
by = {"zz.unknown": [1, 2, 3, 4, 5], "aa.unknown": [1, 2], "can0.17": [1]}
devices = {"can0.17": {"manufacturer": "Airmar", "model": "24xd"}}
check("nothing → None", TW.pick_source({}, "heading", devices) is None)
check("a resolved device wins over coverage", TW.pick_source(by, "heading", devices) == "can0.17")
check("unresolved: most coverage wins", TW.pick_source(by, "heading", {}) == "zz.unknown")
check("a tie is broken by name, so the pick is stable",
      TW.pick_source({"b": [1], "a": [1]}, "heading", {}) == "a")

print("merge_heading_samples:")
hdg = [(100.0, 10.0), (110.0, 11.0), (200.0, 12.0)]
cog = [(95.0, 20.0), (105.0, 21.0)]
sog = [(99.0, 6.0)]
m = TW.merge_heading_samples(hdg, cog, sog)
check("pairs each heading with the freshest cog/sog at or before it",
      m[0] == (100.0, 10.0, 20.0, 6.0) and m[1] == (110.0, 11.0, 21.0, 6.0))
check("older than the 30 s tolerance → None, not a stale value", m[2] == (200.0, 12.0, None, None))
check("no cog/sog series at all → None", TW.merge_heading_samples(hdg, [], []) ==
      [(t, h, None, None) for t, h in hdg])

print("#46.3 a held danger expires:")
# 30 min ok, 30 min at +90° (danger), then 90 min too slow for COG to judge. The hold may carry
# the danger for HEADING_DANGER_HOLD_MIN, then must drop — a held step is not a fresh decision.
slow = heading_track(30, 30) + [(T0 + 3600 + i * 10, (COG + 90) % 360, COG, 0.5)
                                for i in range(1, 90 * 6 + 1)]
hsegs = TW.heading_segments(slow, step_s=60)
for s in hsegs:
    print(f"      {(s['t0'] - T0) / 60:5.0f}-{(s['t1'] - T0) / 60:3.0f} min  {s['status']:<7} {s['reason']}")
dz = TW.danger_intervals(hsegs)
hold_s = sensor_health.HEADING_DANGER_HOLD_MIN * 60
check("the danger does not run to the end of the window", hsegs[-1]["status"] != "danger")
check("…it ends within one hold (+1 step) of the last real danger verdict",
      dz and dz[-1][1] <= T0 + 3600 + sensor_health.HEADING_WINDOW_MIN * 60 + hold_s + 120)
check("…and the hold did carry it past the last decision", dz and dz[-1][1] > T0 + 3600 + 600)

print("#46.4a sources are ranked by their real channel:")
by_path = {"navigation.headingTrue": {"can0.17": [1, 2, 3, 4], "can0.9": [1]},
           "navigation.attitude.roll": {"can0.17": [1, 2, 3, 4], "can0.9": [1]},
           "navigation.attitude.pitch": {"can0.17": [1, 2, 3, 4], "can0.9": [1]}}
dev2 = {"can0.17": {"manufacturer": "Airmar", "model": "24xd"},
        "can0.9": {"manufacturer": "Orca", "model": "orca core"}}
picked = TW.pick_path_sources(by_path, dev2)
check(f"policy lead (orca) beats a better-covered 24xd ({picked})",
      picked == {p: "can0.9" for p in by_path})
check("empty → empty", TW.pick_path_sources({}, dev2) == {})

print("#46.4b roll and pitch need not share an epoch:")
rows = TW.attitude_rows([(T0 + i, 10.0) for i in range(0, 300)],
                        [(T0 + i + 0.5, 50.0 if 120 <= i < 130 else 2.0) for i in range(0, 300)])
psegs = TW.attitude_segments(rows, step_s=60)
check(f"a pitch-only fault shows as danger ({[s['status'] for s in psegs]})",
      "danger" in [s["status"] for s in psegs] and
      any("pitch +50.0°" in s["reason"] for s in psegs if s["status"] == "danger"))
mixed = [(T0 + i, 60.0, 2.0) for i in range(0, 120)] + [(T0 + 30.5, 0.0, 40.0)]
check("a legal big roll does not hide an illegal pitch in the same step",
      "danger" in [s["status"] for s in TW.attitude_segments(sorted(mixed), 60)])
check("pitch-only stream is judged, not unknown",
      [s["status"] for s in TW.attitude_segments(TW.attitude_rows([], [(T0 + i, 2.0) for i in range(130)]), 60)]
      == ["ok"])

print("#46.4c no data is not all-clear:")
e = TW.summarize({"heading": [], "attitude": TW.attitude_segments([], 60)})
print(f"      {e['line']}")
check("empty channels are unchecked, not 'no danger windows'",
      e["line"] != "no danger windows" and e["unchecked"] == ["attitude", "heading"])
u = TW.summarize({"heading": TW.heading_segments(heading_track(60, 0), 60),
                  "attitude": TW.attitude_segments(gap, 60)[:0] + [
                      {"t0": T0, "t1": T0 + 60, "status": "unknown", "reason": "x"}]})
check("one channel all-unknown → named as unchecked", u["unchecked"] == ["attitude"]
      and "attitude not checked" in u["line"])

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
