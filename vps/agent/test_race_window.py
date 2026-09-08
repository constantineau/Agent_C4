"""Race window — would we have found the whole race, given a mis-tapped button?

The fixture is the Jul 18 2026 race exactly as `telemetry_raw` holds it: five-minute means of
SOG and course made good from the Orca Core, 13:30Z Jul 18 to 08:00Z Jul 19. Nothing is
hand-written. What it contains, and why each part is a trap:

  13:30-15:00Z  parked at the dock, SOG 0.00     -- must not be pulled into the race
  15:00-17:00Z  under way, 1.4 -> 4.4 kn         -- delivery to the line / pre-start
  17:03:31Z     SESSION START (a real press)
  18:52:20Z     SESSION END -- the mis-tap, 4.2 s before a four-tap kite hoist on the sail bar
  18:52-00:20Z  racing on, 5-8 kn, COG ~15-26    -- the five hours nobody pressed a button for
  23:20Z        COG swings to 151 for ONE bucket -- the crew wrestling the kicked GPS back in;
                a turnaround detector without a sustain rule ends the race here and loses the
                bank failure, the compass fault and the retirement
  ~00:30Z       the real turnaround, COG -> ~195 and it never comes back -- they retired
  00:30-07:15Z  motoring home at 6-7 kn          -- scrap, and it must not be called racing
  07:15Z+       stopped

The marker says the race is 1 h 49 m. The record says 7 h 27 m. This is the test that has to
tell those apart without being told which is which.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_race_window.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vps", "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import race_window as rw   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


def hhmm(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%m-%d %H:%M")


T0 = 1784382900                      # 2026-07-18T13:35:00Z, the first bucket
# (seconds after T0, SOG kn, course made good deg) -- 5-minute means, straight from telemetry_raw
MOTION_REL = [
    (0,0.08,-7.2), (300,0.0,-2.3), (600,0.0,-2.3), (900,0.0,-2.3),
    (1200,0.0,-2.3), (1500,0.0,-2.3), (1800,0.0,-2.3), (2100,0.0,-2.3),
    (2400,0.0,-2.3), (2700,0.0,-2.3), (3000,0.0,-2.3), (3300,0.0,-2.3),
    (3600,0.0,-2.3), (3900,0.0,-2.3), (4200,0.0,-2.3), (4500,0.0,-2.3),
    (4800,0.0,-2.6), (5100,3.12,1.8), (5400,5.14,-5.3), (5700,5.4,1.4),
    (6000,5.67,-7.0), (6300,5.72,-8.4), (6600,5.19,-10.9), (6900,4.31,-3.9),
    (7200,4.41,-18.4), (7500,4.8,3.8), (7800,5.41,-15.3), (8100,5.52,90.2),
    (8400,2.88,152.5), (8700,3.88,-151.0), (9000,3.63,-98.2), (9300,3.32,-15.0),
    (9600,1.68,-48.5), (9900,2.41,-70.5), (10200,6.41,131.4), (10500,5.4,-15.4),
    (10800,6.61,-46.8), (11100,8.1,-10.6), (11400,8.94,1.4), (11700,8.37,4.4),
    (12000,8.56,6.7), (12300,8.29,6.2), (12600,8.35,3.6), (12900,8.09,0.2),
    (13200,7.96,0.1), (13500,8.23,-0.9), (13800,8.1,4.2), (14100,7.19,10.0),
    (14400,5.53,20.7), (14700,5.98,8.7), (15000,4.49,14.1), (15300,2.61,20.3),
    (15600,5.78,3.6), (15900,6.67,3.9), (16200,6.22,4.1), (16500,6.78,-1.3),
    (16800,6.82,-3.1), (17100,6.61,-3.0), (17400,5.87,-2.0), (17700,6.99,-4.8),
    (18000,6.65,-1.9), (18300,6.53,-1.4), (18600,5.36,1.5), (18900,4.41,8.8),
    (19200,6.37,10.6), (19500,6.88,4.9), (19800,6.32,23.5), (20100,6.12,22.8),
    (20400,6.22,26.5), (20700,6.21,29.4), (21000,6.64,45.6), (21300,6.24,48.1),
    (21600,6.35,52.6), (21900,6.31,50.4), (22200,6.25,46.4), (22500,6.23,41.4),
    (22800,6.21,43.0), (23100,6.22,41.3), (23400,6.11,40.5), (23700,6.03,40.4),
    (24000,6.18,39.7), (24300,6.14,39.2), (24600,5.75,33.6), (24900,5.46,31.7),
    (25200,5.59,29.4), (25500,5.59,25.3), (25800,5.3,26.9), (26100,5.39,21.7),
    (26400,5.38,25.9), (26700,5.73,19.4), (27000,5.45,15.5), (27300,5.85,16.4),
    (27600,5.8,22.2), (27900,5.7,12.5), (28200,6.03,14.1), (28500,6.15,17.2),
    (28800,5.46,12.9), (29100,6.06,25.6), (29400,6.03,15.4), (29700,5.87,17.9),
    (30000,6.08,14.3), (30300,5.51,9.4), (30600,5.83,18.4), (30900,5.51,17.0),
    (31200,5.95,14.7), (31500,5.96,25.0), (31800,5.52,18.6), (32100,6.11,14.4),
    (32400,5.72,19.4), (32700,6.09,18.3), (33000,5.91,17.1), (33300,6.08,14.9),
    (33600,5.73,13.6), (33900,2.68,36.3), (34200,2.82,-140.0), (34500,3.1,-158.5),
    (34800,2.91,85.4), (35100,4.77,22.0), (35400,5.84,20.2), (35700,5.15,27.5),
    (36000,5.08,26.4), (36300,5.51,25.7), (36600,6.16,27.5), (36900,5.51,24.2),
    (37200,6.04,24.7), (37500,6.21,25.6), (37800,5.56,28.9), (38100,1.19,124.2),
    (38400,1.59,79.8), (38700,6.03,134.4), (39000,7.05,162.2), (39300,6.99,-166.7),
    (39600,7.43,-170.2), (39900,8.35,-166.5), (40200,8.39,-174.1), (40500,7.46,-160.9),
    (40800,7.94,-162.9), (41100,7.22,-168.1), (41400,7.45,-170.5), (41700,7.44,-164.7),
    (42000,7.28,-173.4), (42300,7.6,-174.5), (42600,8.71,-167.7), (42900,7.4,-165.5),
    (43200,8.09,-161.7), (43500,8.11,-161.2), (43800,7.07,-170.7), (44100,7.78,-154.7),
    (44400,6.97,-163.8), (44700,7.7,-173.1), (45000,7.64,-158.1), (45300,7.11,-158.2),
    (45600,7.13,-158.8), (45900,7.25,-158.6), (46200,7.83,-156.1), (46500,7.05,-159.7),
    (46800,7.5,-161.6), (47100,6.63,-158.9), (47400,7.19,-154.3), (47700,6.72,-154.2),
    (48000,7.17,-156.5), (48300,6.88,-162.5), (48600,6.72,-163.7), (48900,6.55,-166.8),
    (49200,6.57,-169.3), (49500,6.69,-163.4), (49800,6.81,-159.1), (50100,6.76,-167.4),
    (50400,7.96,-161.1), (50700,7.24,-163.0), (51000,6.66,-170.6), (51300,6.9,-166.3),
    (51600,6.0,-163.4), (51900,7.15,-162.6), (52200,6.83,-167.0), (52500,5.77,-161.0),
    (52800,6.16,-161.6), (53100,6.47,-162.3), (53400,7.1,-162.9), (53700,6.58,-162.1),
    (54000,6.67,-164.6), (54300,6.77,-162.5), (54600,6.7,-161.0), (54900,7.18,-156.1),
    (55200,7.11,-159.0), (55500,6.68,-157.2), (55800,6.31,-156.7), (56100,6.44,-157.7),
    (56400,7.24,-152.7), (56700,7.07,-157.0), (57000,6.91,-157.1), (57300,6.95,-157.0),
    (57600,7.16,-157.2), (57900,7.39,-158.6), (58200,7.3,-162.4), (58500,7.73,-162.6),
    (58800,7.15,-156.7), (59100,6.51,-158.4), (59400,7.23,-163.3), (59700,6.99,-158.3),
    (60000,7.45,-159.2), (60300,6.64,-164.3), (60600,7.53,-162.9), (60900,7.27,-159.2),
    (61200,7.4,-160.2), (61500,7.49,-161.5), (61800,8.0,-162.7), (62100,6.8,-148.5),
    (62400,1.95,178.1), (62700,0.0,146.8), (63000,0.0,107.2), (63300,0.0,0.0),
    (63600,0.0,0.0), (63900,0.0,0.0), (64200,0.0,0.0), (64500,0.03,-9.6),
    (64800,0.02,-20.6),
]
MOTION = [(T0 + dt, sog, cog) for (dt, sog, cog) in MOTION_REL]

# The session marker exactly as the boat wrote it (crew.session id 2).
MARKER = {"id": 2, "race_id": "bayview-mackinac-2026", "kind": "race",
          "start_ts": 1784394211.45, "end_ts": 1784400740.81}

# --- the headline: the marker is wrong and the record knows it ---------------
print("the Jul 18 race, from a marker that stopped mid-kite-hoist:")
w = rw.derive([MARKER], MOTION)
check(f"the marker claims {w['marker_hours']} h", w["marker_hours"] == 1.81)
check(f"the derived window is {w['hours']} h ({hhmm(w['start_ts'])} -> {hhmm(w['end_ts'])})",
      6.5 <= w["hours"] <= 8.0)
check("...and it starts where the crew pressed start, not out in the delivery",
      w["start_ts"] == MARKER["start_ts"])
check("...and it keeps the marker alongside, so nobody has to trust the derivation blind",
      w["marker_start_ts"] == MARKER["start_ts"] and w["marker_end_ts"] == MARKER["end_ts"])
check("...and says WHY, in words a person can check", len(w["provenance"]) >= 2)
for p in w["provenance"]:
    print(f"         · {p}")

# --- the turnaround is the end of the race, and the 23:20Z transient is not --
print("the turnaround:")
t = w["turnaround"]
check(f"found, and it is a reversal not a wobble ({t['change_deg']:.0f}°, "
      f"{t['from_deg']:.0f}° -> {t['to_deg']:.0f}°)", t and t["change_deg"] >= 120)
check(f"it is at {hhmm(t['ts'])} — the retirement, ~20:00 local, not the 23:20Z GPS wrestle",
      1784420000 < t["ts"] < 1784424000)
check("the race ends at or before it — the delivery home is excluded",
      w["end_ts"] <= t["ts"])
check("...and 6+ hours of motoring home did NOT become 'the race'",
      w["end_ts"] < T0 + 60000)
# The 23:20Z transient, stated as its own assertion. It survives a sustain rule 4.5x shorter
# than the default AND a 30-deg-lower threshold, because the 30-min reference average on each
# side smooths a single 5-min bucket away. Belt and braces, but worth knowing which one is
# actually carrying the load: it is the averaging, not the sustain.
early = rw.find_turnaround(MOTION, MARKER["end_ts"], sustain_s=600, min_deg=90)
check("the 23:20Z GPS-wrestle transient does NOT end the race, even at a 10-min sustain and 90 deg",
      early is not None and early["ts"] >= t["ts"] - 600)

# --- stitching -------------------------------------------------------------
print("stitching mis-taps back together:")
mistap = [{"id": 2, "start_ts": 1784394211.0, "end_ts": 1784400740.0},
          {"id": 3, "start_ts": 1784400920.0, "end_ts": 1784410000.0}]   # restarted 3 min later
s = rw.stitch(mistap)
check("a stop and a restart 3 min apart is ONE race", len(s) == 1)
check("...spanning both", s[0]["start_ts"] == 1784394211.0 and s[0]["end_ts"] == 1784410000.0)
check("...and remembers which markers it came from", s[0]["ids"] == [2, 3])
far = [{"id": 1, "start_ts": 1784155457.0, "end_ts": 1784160147.0},     # Jul 15
       {"id": 2, "start_ts": 1784394211.0, "end_ts": 1784400740.0}]     # Jul 18
check("two races three days apart are NOT stitched", len(rw.stitch(far)) == 2)
check("a session still open (no end_ts) survives as a point, never dropped",
      len(rw.stitch([{"id": 9, "start_ts": 1784394211.0, "end_ts": None}])) == 1)
check("no sessions at all is None, not a guess", rw.derive([], MOTION) is None)
check("a marker with no start_ts is skipped rather than crashing",
      rw.stitch([{"id": 1, "start_ts": None, "end_ts": 5.0}]) == [])

# --- the extension must not swallow the dock or the delivery ----------------
print("the edges:")
span = rw.underway_span(MOTION, MARKER["start_ts"])
check(f"the underway run around the start begins at {hhmm(span[0])}, after the boat left the dock",
      span[0] >= T0 + 4800)
check("opting in to extend_start reaches back to it, for whoever wants the pre-start",
      rw.derive([MARKER], MOTION, extend_start=True)["start_ts"] < MARKER["start_ts"])
check("the dock hours (SOG 0.00) are outside it", span[0] > T0 + 3600)
check("a timestamp while parked at the dock has no underway span",
      rw.underway_span(MOTION, T0 + 600) is None)
check("an empty motion series is None, not an exception", rw.underway_span([], MARKER["start_ts"]) is None)
# The bound changes nothing on THIS race: they slowed below 1.5 kn to douse before turning, so
# the underway run had already ended. That is luck, not design — a boat that gybes away carrying
# its speed sails straight past the end of the race and only the turnaround catches it.
check("on Jul 18 the underway test alone happens to stop at the same place",
      rw.derive([MARKER], MOTION, stop_at_turnaround=False)["hours"] == w["hours"])
faster = [(t_, max(sog, 5.0), cog) for (t_, sog, cog) in MOTION]     # same race, no douse
_unbounded = rw.derive([MARKER], faster, stop_at_turnaround=False)["hours"]
_bounded = rw.derive([MARKER], faster)["hours"]
check(f"...but with the douse removed it would swallow the delivery ({_unbounded} h), and the "
      f"turnaround bound stops it ({_bounded} h)",
      _unbounded > 14 and _bounded < 9 and _unbounded - _bounded > 6)

# --- race_id filtering ------------------------------------------------------
print("race_id:")
check("filtering by race_id keeps only that race's markers",
      rw.derive([MARKER], MOTION, race_id="bayview-mackinac-2026") is not None)
check("...and an unknown race_id yields None rather than the wrong race",
      rw.derive([MARKER], MOTION, race_id="some-other-race") is None)

# --- the wrap, because this project has been bitten by it -------------------
print("angles:")
check("the circular mean of 359 and 1 is ~0, not 180", abs(rw._mean_angle([359.0, 1.0])) < 1)
check("a reversal across north is still a reversal",
      abs(rw._wrap180(10.0 - 190.0)) == 180.0)

# --- which source the window is read off --------------------------------------
# One source, for the same reason the heading cross-check uses one: a series alternating between
# two GPSs is a series of manufactured turns, and find_turnaround would read them as a retirement.
print("the motion source:")
from shared import n2k_sources as ns   # noqa: E402
DEV = ns.devices_for("sr33")
ORCA, G24XD = "n2k-socketcan.15", "n2k-socketcan.3"
check("the rank-1 GPS wins over the rank-2 one",
      rw.choose_motion_source({ORCA: [1] * 300, G24XD: [1] * 366}, DEV) == (G24XD, True))
check("a real device beats a bench source with 18x the coverage — 'unranked' and 'not this "
      "boat' are different problems",
      rw.choose_motion_source({"n2k-sample-data.160": [1] * 999, ORCA: [1] * 55}, DEV)
      == (ORCA, True))
check("...and when only bench sources exist it still returns one, flagged unresolved, rather "
      "than refusing (this is the real Jul 8 2026 window)",
      rw.choose_motion_source({"n2k-sample-data.160": [1] * 56, "fake-seed": [1] * 5}, DEV)
      == ("n2k-sample-data.160", False))
check("no publishers at all is (None, False), not a crash",
      rw.choose_motion_source({}, DEV) == (None, False))
check("coverage breaks a tie between two equally-ranked unknown sources",
      rw.choose_motion_source({"a-src": [1] * 10, "b-src": [1] * 99}, DEV)[0] == "b-src")

print("\nthe race, as the window sees it:")
print(f"  marker   {hhmm(w['marker_start_ts'])} -> {hhmm(w['marker_end_ts'])}  {w['marker_hours']:>5} h")
print(f"  derived  {hhmm(w['start_ts'])} -> {hhmm(w['end_ts'])}  {w['hours']:>5} h")

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
