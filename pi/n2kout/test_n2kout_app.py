"""n2kout — live leg, expiry/clear/course change, POST /route validation, the bus thread (ISO
requests, address-claim contest, restart) and the bus diagnostics. No CAN: fake sender/socket.

Run:  PYTHONPATH=pi/n2kout python3 pi/n2kout/test_n2kout_app.py
"""
import json
import math
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, "pi/n2kout")
import n2k  # noqa: E402
import n2kout_app as A  # noqa: E402
import navcalc  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


M_LAT = 111195.0                        # metres per degree of latitude (R = 6371008.8)


def east(lat, m):                       # degrees of longitude for m metres east at lat
    return m / (M_LAT * math.cos(math.radians(lat)))


# a route north 2 legs, then east: wp0 → wp1 → wp2 → wp3
WP = [{"name": "C4-01", "lat": 43.00, "lon": -82.0}, {"name": "C4-02", "lat": 43.01, "lon": -82.0},
      {"name": "C4-03", "lat": 43.02, "lon": -82.0},
      {"name": "C4-04", "lat": 43.02, "lon": -82.0 + east(43.02, 1500)}]

print("navcalc — the waypoint advances as the boat moves:")
tr = navcalc.LegTracker(WP, 1)
n = tr.update(43.002, -82.0, 0)
check("short of wp 1 → steer to 1", n["dest_index"] == 1)
n = tr.update(43.0095, -82.0 + east(43.0095, 400), 10)
check("abeam 400 m off, 55 m short of wp 1 → still 1", n["dest_index"] == 1)
n = tr.update(43.0103, -82.0 + east(43.0103, 400), 20)
check("passed the turn line at wp 1 (wide, outside 0.05 nm) → 2", n["dest_index"] == 2)
n = tr.update(43.0005, -82.0, 30)
check("never steps back (boat sails back south)", n["dest_index"] == 2)
n = tr.update(43.0196, -82.0, 40)
check("within 0.05 nm of wp 2 → 3", n["dest_index"] == 3 and tr.advances == 2)
n = tr.update(43.02, -82.0 + east(43.02, 1460), 50)
check("last waypoint: stays on it, arrival flag set", n["dest_index"] == 3 and n["arrived"])
n = tr.update(43.02, -82.0 + east(43.02, 1700), 60)
check("past the last one: perpendicular crossed, still on it", n["dest_index"] == 3
      and n["perp_crossed"] and not n["arrived"])

# the turn at wp 2 is 90°: the bisector, not the perpendicular, decides
tr = navcalc.LegTracker(WP, 2)
n = tr.update(43.0185, -82.0 + east(43.0185, 300), 0)
check("90° turn: across the bisector (300 m E, 167 m short) → advances", n["dest_index"] == 3)
tr = navcalc.LegTracker(WP, 2)
n = tr.update(43.0185, -82.0 - east(43.0185, 300), 0)
check("same distance short but on the other side → not advanced", n["dest_index"] == 2)

# hairpin: up to B and straight back down
HP = [{"lat": 43.0, "lon": -82.0}, {"lat": 43.01, "lon": -82.0},
      {"lat": 43.0, "lon": -82.0 + east(43.0, 20)}]
tr = navcalc.LegTracker(HP, 1)
check("hairpin: below the mark → 1", tr.update(43.008, -82.0 + east(43.008, 150), 0)["dest_index"] == 1)
check("hairpin: above the mark (wide) → 2", tr.update(43.0104, -82.0 + east(43.0104, 150), 1)["dest_index"] == 2)

print("navcalc — course marks advance on the arrival radius only:")
WPM = [dict(w) for w in WP]
WPM[1]["mark"] = True
tr = navcalc.LegTracker(WPM, 1)
n = tr.update(43.0103, -82.0 + east(43.0103, 400), 0)
check("past a MARK's turn line 400 m wide → still steering to it", n["dest_index"] == 1)
n = tr.update(43.0102, -82.0 + east(43.0102, 30), 1)
check("within 0.05 nm of the mark → advances", n["dest_index"] == 2)


def local(lat0, lon0):
    """(x east, y north) metres → (lat, lon) around a point."""
    return lambda x, y: (lat0 + y / M_LAT, lon0 + east(lat0, x))


print("navcalc — every new leg is approved by the engine (Codex repro):")
L = local(43.0, -82.0)
CX = [dict(zip(("lat", "lon"), L(0, -500))), dict(zip(("lat", "lon"), L(0, 80))),
      dict(zip(("lat", "lon"), L(80, 80)))]
ROCK = L(40, 40)


def seg_clear(a, b, c, r=20.0):
    """Does segment a→b stay r metres clear of point c? (flat metres around a)"""
    k = east(43.0, 1)
    bx, by = (b[1] - a[1]) / k, (b[0] - a[0]) * M_LAT
    cx, cy = (c[1] - a[1]) / k, (c[0] - a[0]) * M_LAT
    L2 = bx * bx + by * by
    u = max(0.0, min(1.0, (cx * bx + cy * by) / L2)) if L2 else 0.0
    return math.hypot(cx - u * bx, cy - u * by) >= r


calls = []


def approve_rock(frm, idx):
    calls.append(idx)
    return seg_clear(frm, (CX[idx]["lat"], CX[idx]["lon"]), ROCK)


tr = navcalc.LegTracker(CX, 1)
n = tr.update(*L(0, 0), 0, approve=approve_rock)
check("boat 80 m from the waypoint (inside 0.05 nm), next leg through the rock → NOT advanced",
      n["dest_index"] == 1 and n["blocked"] and calls == [2])
n = tr.update(*L(0, 1), 2, approve=approve_rock)
check("refused → not asked again before the retry (5 s)", calls == [2] and n["blocked"])
n = tr.update(*L(0, 2), 6, approve=approve_rock)
check("retried after 5 s, still blocked", calls == [2, 2] and n["blocked"]
      and n["dest_index"] == 1)
n = tr.update(*L(-60, 80), 12, approve=approve_rock)
check("boat moves so the line to the next waypoint clears → advances, unblocked",
      n["dest_index"] == 2 and not n["blocked"])
tr = navcalc.LegTracker(CX, 1)
n = tr.update(*L(0, 0), 0, approve=lambda frm, idx: None)
check("engine doesn't answer → not advanced, blocked", n["dest_index"] == 1 and n["blocked"])

print("navcalc — the current leg is re-checked every 30 s:")
state = {"clear": True}
calls.clear()


def approve_flag(frm, idx):
    calls.append(idx)
    return state["clear"]


tr = navcalc.LegTracker(WP, 1)
tr.update(43.001, -82.0, 0, approve=approve_flag)
tr.update(43.002, -82.0, 29, approve=approve_flag)
check("no re-check before 30 s (the send checked the first leg)", calls == [])
state["clear"] = False
n = tr.update(43.003, -82.0, 31, approve=approve_flag)
check("30 s: current leg re-checked, now blocked → paused", calls == [1] and n["blocked"])
state["clear"] = True
n = tr.update(43.003, -82.0, 37, approve=approve_flag)
check("clear again at the retry → resumed", calls == [1, 1] and not n["blocked"])

print("navcalc — one advance (one approval) per update:")
calls = []
tr = navcalc.LegTracker(WP, 1)
n = tr.update(43.02, -82.0 + east(43.02, 1460), 0,
              approve=lambda frm, idx: calls.append(idx) or True)
check("boat already past wp 1 and 2 → ONE approval, ONE advance this update",
      calls == [2] and n["dest_index"] == 2)
n = tr.update(43.02, -82.0 + east(43.02, 1460), 1,
              approve=lambda frm, idx: calls.append(idx) or True)
check("…and the next one on the next update", calls == [2, 3] and n["dest_index"] == 3)

print("navcalc — a wide mark rounding advances on the navigator's verdict:")
WPR = [dict(w) for w in WP]
WPR[1].update(mark=True, mark_idx=2)            # course mark #2 (the navigator's numbering)
tr = navcalc.LegTracker(WPR, 1)
wide = (43.0103, -82.0 + east(43.0103, 800))     # 800 m out: rounded wide, past its line
check("navigator's next mark is still this one (2) → stays",
      tr.update(*wide, 0, next_mark_index=2)["dest_index"] == 1)
check("no navigator verdict → stays", tr.update(*wide, 1)["dest_index"] == 1)
asked = []
n = tr.update(*wide, 2, next_mark_index=3, approve=lambda frm, idx: asked.append(idx) or False)
check("navigator says rounded (next 3) → advance asked, refused → stays, blocked",
      asked == [2] and n["dest_index"] == 1 and n["blocked"])
n = tr.update(*wide, 8, next_mark_index=3, approve=lambda frm, idx: True)
check("…approved → advances to the next waypoint", n["dest_index"] == 2 and not n["blocked"])
WPN = [dict(w) for w in WP]
WPN[1].update(mark=True)                         # a mark with no known course position
tr = navcalc.LegTracker(WPN, 1)
check("mark without mark_idx → the navigator can't move it (radius only)",
      tr.update(*wide, 0, next_mark_index=9)["dest_index"] == 1)

print("navcalc — XTE is real and signed:")
tr = navcalc.LegTracker(WP, 1)
r = tr.update(43.005, -82.0 + east(43.005, 100), 0)["xte_m"]
check(f"100 m east of a northbound leg → +100 m (right) [{r:.2f}]", abs(r - 100) < 0.5)
r = tr.update(43.005, -82.0 - east(43.005, 37), 1)["xte_m"]
check(f"37 m west → -37 m (left) [{r:.2f}]", abs(r + 37) < 0.5)
check("on the line → ~0", abs(tr.update(43.006, -82.0, 2)["xte_m"]) < 0.05)
tr = navcalc.LegTracker(WP, 0)
n = tr.update(42.99, -82.0 + east(42.99, 500), 0)
check("leg to wp 0 starts at the first fix: XTE 0 there, not n/a", n["xte_m"] is not None
      and abs(n["xte_m"]) < 0.01 and n["dest_index"] == 0)

print("navcalc — distance, bearing, ETA from the closing rate:")
tr = navcalc.LegTracker(WP, 1)
lat0 = 43.0
for k in range(0, 61, 5):
    n = tr.update(lat0 + 5 * k / M_LAT, -82.0, 1000 + k)
    if k == 10:
        check("ETA n/a before 20 s of history", n["eta_epoch"] is None)
check(f"DTW live [{n['dtw_m']:.0f} m]", abs(n["dtw_m"] - (0.01 * M_LAT - 300)) < 2)
check("bearing to a waypoint due north ≈ 0°", min(n["brg_deg"], 360 - n["brg_deg"]) < 0.1)
check(f"closing 5 m/s [{n['closing_mps']:.2f}]", abs(n["closing_mps"] - 5) < 0.05)
check("ETA = now + DTW / closing", abs(n["eta_epoch"] - (1060 + n["dtw_m"] / 5)) < 3)
tr = navcalc.LegTracker(WP, 1)
for k in range(0, 61, 5):
    n = tr.update(43.002, -82.0 + east(43.002, k), k)       # sailing across, not closing
check("not closing → ETA n/a", n["eta_epoch"] is None)


# --- the broadcaster -----------------------------------------------------------------------
class FakeSender:
    def __init__(self):
        self.sent, self.sa, self.frames_sent, self.send_errors = [], 35, 0, 0
        self.last_error = self.last_error_at = self.last_ok_at = None
        self._lock = threading.Lock()

    def send(self, pgn, prio, data, single=None, dest=0xFF, src=None):
        self.sent.append((pgn, data, dest, self.sa if src is None else src))
        return 1

    def retry_claim(self):
        self.retries = getattr(self, "retries", 0) + 1
        return True

    def change_address(self, new, claim):
        self.sa = new
        self.sent.append((claim[0], claim[2], 0xFF, new))
        return 1

    def pgns(self):
        return [p for p, *_ in self.sent]


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def body(**kw):
    d = {"name": "C4 left", "waypoints": WP, "dest_index": 1, "course": "L1:race"}
    d.update(kw)
    return A.RouteBody(**d)


def CLEAR(frm, to, course):
    return True


def broadcaster(feed):
    fs, clk = FakeSender(), Clock()
    b = A._Broadcaster(sender=fs, fix_source=lambda: feed["v"], clock=clk,
                       leg_check=CLEAR)
    b.state["expires_at"] = clk.t + A.ROUTE_TTL_S
    return b, fs, clk


print("broadcaster — live nav every cycle:")
feed = {"v": None}
b, fs, clk = broadcaster(feed)
B = body()
tr = navcalc.LegTracker(WP, 1)
feed["v"] = {"fix": {"lat": 43.005, "lon": -82.0 + east(43.005, 50), "t": clk.t - 1},
             "course": "L1:race"}
check("fresh fix → keeps going", b.cycle(B, tr, 0) is None)
check("sends 129283 + 129284", fs.pgns() == [129283, 129284])
xte = struct.unpack("<i", fs.sent[0][1][2:6])[0]
check(f"the XTE on the wire is the real one (+50 m → {xte / 100:.2f})", abs(xte - 5000) < 50)
check("dest wp 1 on the wire", struct.unpack("<I", fs.sent[1][1][20:24])[0] == 1)
check("status shows the live leg", b.state["nav_live"] and b.state["nav"]["dest"] == "C4-02")
fs.sent.clear()
feed["v"]["fix"] = {"lat": 43.0103, "lon": -82.0 + east(43.0103, 300), "t": clk.t}
b.cycle(B, tr, 1)
check("boat past wp 1 → the next cycle steers to wp 2",
      struct.unpack("<I", fs.sent[1][1][20:24])[0] == 2 and b.state["nav"]["dest"] == "C4-03")

print("broadcaster — a stale or missing position stops the nav data:")
for label, v in (("fix 11 s old", {"fix": {"lat": 43.01, "lon": -82.0, "t": clk.t - 11},
                                   "course": "L1:race"}),
                 ("no fix", {"fix": None, "course": "L1:race"}),
                 ("engine unreachable", None),
                 ("garbage fix", {"fix": {"lat": "x", "lon": 1, "t": clk.t}, "course": "L1:race"}),
                 ("NaN fix", {"fix": {"lat": float("nan"), "lon": 1, "t": clk.t},
                              "course": "L1:race"}),
                 ("fix from the future", {"fix": {"lat": 43.01, "lon": -82.0, "t": clk.t + 60},
                                          "course": "L1:race"})):
    fs.sent.clear()
    feed["v"] = v
    r = b.cycle(B, tr, 2)
    check(f"{label} → no 129283/129284, route not ended", r is None and fs.sent == []
          and not b.state["nav_live"])

print("broadcaster — a leg the engine doesn't clear pauses the nav data:")
fs2, clk2 = FakeSender(), Clock()
asked = []
b2 = A._Broadcaster(sender=fs2, clock=clk2,
                    fix_source=lambda: {"fix": {"lat": L(0, 0)[0], "lon": L(0, 0)[1],
                                                "t": clk2.t}, "course": "L1:race"},
                    leg_check=lambda frm, to, course: asked.append((to, course)) or False)
b2.state["expires_at"] = clk2.t + 100
BX = A.RouteBody(name="C4 x", waypoints=CX, dest_index=1, course="L1:race")
trx = navcalc.LegTracker([w.model_dump() for w in BX.waypoints], 1)
r = b2.cycle(BX, trx, 0)
check("blocked → route goes on, but no 129283/129284 sent", r is None and fs2.sent == [])
check("status: steer-to paused (blocked), not live", b2.state["steer_paused"] == "blocked"
      and not b2.state["nav_live"])
check("asked the engine about boat → waypoint 2 with the course token",
      asked and abs(asked[0][0][0] - CX[2]["lat"]) < 1e-9 and asked[0][1] == "L1:race")
b2.leg_check = lambda frm, to, course: True
clk2.t += 6
fs2.sent.clear()
b2.cycle(BX, trx, 1)
check("cleared → advances and sends the nav data again", fs2.pgns() == [129283, 129284]
      and b2.state["steer_paused"] is None and b2.state["nav"]["dest_index"] == 2)
r = A.RouteBody(name="x", waypoints=[{"lat": 1, "lon": 1, "mark": True}, {"lat": 2, "lon": 2}])
check("POST /route accepts the mark flag", r.waypoints[0].mark is True)

_s = socket.socket()
_s.bind(("127.0.0.1", 0))
A.LEG_CHECK_URL = f"http://127.0.0.1:{_s.getsockname()[1]}/gps/leg-check"
_s.close()
check("the real leg check with no engine → (None, 'no answer') (treated as blocked)",
      A.check_leg((43.0, -82.0), (43.1, -82.0), "L1:race") == (None, "no answer"))
fs8, clk8 = FakeSender(), Clock()
b8 = A._Broadcaster(sender=fs8, clock=clk8,
                    leg_check=lambda frm, to, course: (False, "chart unreadable"),
                    fix_source=lambda: {"fix": {"lat": L(0, 0)[0], "lon": L(0, 0)[1],
                                                "t": clk8.t}, "course": "L1:race"})
b8.state["expires_at"] = clk8.t + 100
tr8 = navcalc.LegTracker([w.model_dump() for w in BX.waypoints], 1)
b8.cycle(BX, tr8, 0)
check("engine says chart unreadable → nothing sent, steer_paused 'chart unreadable'",
      fs8.sent == [] and b8.state["steer_paused"] == "chart unreadable")
clk8.t += 2                                       # within the retry: no new answer
b8.cycle(BX, tr8, 1)
check("…the reason holds while waiting for the retry",
      b8.state["steer_paused"] == "chart unreadable")
b8.leg_check = lambda frm, to, course: (False, "obstacle")
clk8.t += 5
b8.cycle(BX, tr8, 2)
check("an obstacle refusal → plain 'blocked'", b8.state["steer_paused"] == "blocked")

print("broadcaster — expiry and course change end the route:")
feed["v"] = {"fix": {"lat": 43.005, "lon": -82.0, "t": clk.t}, "course": "L2:race"}
check("course token changed → 'course changed'", b.cycle(B, tr, 3) == "course changed")
feed["v"] = {"fix": None, "course": None}
check("course gone → 'course changed'", b.cycle(B, tr, 3) == "course changed")
feed["v"] = {"fix": None, "course": "L1:race"}
clk.t = b.state["expires_at"] + 1
check("past expires_at → 'expired'", b.cycle(B, tr, 3) == "expired")


def run_until(b, cond, timeout=3.0):
    t0 = time.time()
    while time.time() - t0 < timeout and not cond():
        time.sleep(0.02)
    return cond()


def terminated(fs):
    t = [d for p, d, *_ in fs.sent if p == 129283 and d[1] == 0x70]
    nulls = [d for p, d, *_ in fs.sent if p == 129284
             and struct.unpack("<I", d[20:24])[0] == n2k.NA_U32]
    return len(t) >= 1 and len(nulls) >= 1


A.NAV_EVERY_S = 0.05
A.TERMINATE_REPEATS = 2
print("broadcast thread — clear, expiry, course change send 'navigation terminated':")
feed = {"v": {"fix": {"lat": 43.005, "lon": -82.0, "t": time.time()}, "course": "L1:race"}}
fs = FakeSender()
b = A._Broadcaster(sender=fs, fix_source=lambda: feed["v"], leg_check=CLEAR)
b.start(body())
run_until(b, lambda: 129285 in fs.pgns())
check("broadcasting: route list + live nav", b.state["broadcasting"] and 129285 in fs.pgns()
      and 129283 in fs.pgns())
fs.sent.clear()
b.stop("cleared")
check("Clear → stopped, terminated + null nav sent", not b.state["broadcasting"]
      and b.state["ended"] == "cleared" and terminated(fs))
n_after = len(fs.sent)
time.sleep(0.15)
check("nothing more after Clear", len(fs.sent) == n_after)

ttl0 = A.ROUTE_TTL_S
A.ROUTE_TTL_S = 0.2
fs.sent.clear()
b.start(body())
run_until(b, lambda: not b.state["broadcasting"])
check("expiry → stopped by itself, 'expired', terminated", not b.state["broadcasting"]
      and b.state["ended"] == "expired" and terminated(fs))
A.ROUTE_TTL_S = ttl0

fs.sent.clear()
b.start(body())
run_until(b, lambda: 129283 in fs.pgns())
feed["v"] = {"fix": {"lat": 43.005, "lon": -82.0, "t": time.time()}, "course": "L2:race"}
run_until(b, lambda: not b.state["broadcasting"])
check("course load (new token on the fix feed) → stopped, terminated",
      not b.state["broadcasting"] and b.state["ended"] == "course changed" and terminated(fs))

print("broadcast thread — a stuck worker never sends after Stop, never kills the next route:")
A.JOIN_S = 0.3
gate = threading.Event()
inside = threading.Event()
fsx = FakeSender()
feedx = {"fix": {"lat": 43.0103, "lon": -82.0 + east(43.0103, 300), "t": time.time()},
         "course": "L1:race"}


def slow_check(frm, to, course):                # the engine hangs on a leg check
    inside.set()
    gate.wait(5)
    return True


def live_feed():
    feedx["fix"]["t"] = time.time()
    return feedx


bx = A._Broadcaster(sender=fsx, fix_source=live_feed, leg_check=slow_check)
bx.start(body())
inside.wait(3)
check("(setup) the worker is inside a leg check", inside.is_set())
ok_stop = bx.stop("cleared")
n_at_stop = len(fsx.sent)
check("Stop with a stuck worker → reports it didn't exit", ok_stop is False
      and bx.state["last_error"] == "Previous broadcast didn't stop.")
check("Stop still said 'navigation terminated'", terminated(fsx))
try:
    bx.start(body())
    check("a new route while the old worker is stuck → refused", False)
except A.StuckWorker as e:
    check("a new route while the old worker is stuck → refused", "didn't stop" in str(e))
gate.set()                                       # the engine answers: the old worker wakes
run_until(bx, lambda: not bx.thread.is_alive(), 3)
time.sleep(0.2)
late = fsx.sent[n_at_stop:]
check("the old worker sent NOTHING after Stop returned (no nav, no route, no terminate)",
      late == [])
check("…and wrote no status (still stopped, no live nav)", not bx.state["broadcasting"]
      and not bx.state["nav_live"] and bx.state["nav"] is None)
bx.leg_check = CLEAR
fsx.sent.clear()
bx.start(body())
run_until(bx, lambda: 129283 in fsx.pgns())
check("once it has exited, a new route starts", bx.state["broadcasting"]
      and 129283 in fsx.pgns())
check("…and nobody terminated it", not terminated(fsx))
g_old = bx.gen
bx.stop()
bx._set(g_old, broadcasting=True, nav_live=True)
check("a status write from a retired generation is ignored", not bx.state["broadcasting"]
      and not bx.state["nav_live"])

print("broadcast thread — Stop is quick; a course change's stop keeps the new course's route:")
gate2, inside2 = threading.Event(), threading.Event()


def hang(frm, to, course):
    inside2.set()
    gate2.wait(5)
    return True


fs5 = FakeSender()
b5 = A._Broadcaster(sender=fs5, fix_source=live_feed, leg_check=hang)
b5.start(body())
inside2.wait(3)
t0 = time.time()
b5.stop("cleared")
check(f"Stop with a stuck worker returns in about a second [{time.time() - t0:.2f} s]",
      time.time() - t0 < 1.5)
gate2.set()
run_until(b5, lambda: not b5.thread.is_alive(), 3)
b5.leg_check = CLEAR
b5.start(body(course="B1:race"))
check("keep_course = this route's course → kept", b5.stop(keep_course="B1:race") is True
      and b5.state["broadcasting"])
b5.stop(reason="course changed", keep_course="C1:race")
check("keep_course = another course → stopped, 'course changed'",
      not b5.state["broadcasting"] and b5.state["ended"] == "course changed")

print("broadcaster — nav_live only when the nav PGNs really reached the bus:")
real = n2k.N2kSender("can-nope")               # no such interface: every send fails


clk6 = Clock()
b6 = A._Broadcaster(sender=real, clock=clk6, leg_check=CLEAR,
                    fix_source=lambda: {"fix": {"lat": 43.005, "lon": -82.0, "t": clk6.t},
                                        "course": "L1:race"})
b6.state["expires_at"] = clk6.t + 1000
b6.cycle(body(), navcalc.LegTracker(WP, 1), 0)
check("CAN down → sends fail → NOT nav_live, 'not sent', the error counted",
      not b6.state["nav_live"] and b6.state["steer_paused"] == "not sent"
      and real.send_errors >= 1 and real.frames_sent == 0)
check("…and the iPad's line isn't 'sending'",
      A.link_word({"present": True, "up": True, "state": "ERROR-ACTIVE"}, real, True,
                  time.time()) == "errors")


class HalfSender(FakeSender):                      # 129283 goes, 129284 fails
    def send(self, pgn, prio, data, single=None, dest=0xFF, src=None):
        return 0 if pgn == 129284 else super().send(pgn, prio, data)


b7 = A._Broadcaster(sender=HalfSender(), clock=clk6, leg_check=CLEAR,
                    fix_source=lambda: {"fix": {"lat": 43.005, "lon": -82.0, "t": clk6.t},
                                        "course": "L1:race"})
b7.state["expires_at"] = clk6.t + 1000
b7.cycle(body(), navcalc.LegTracker(WP, 1), 0)
check("only one of the two nav PGNs went out → not live", not b7.state["nav_live"]
      and b7.state["steer_paused"] == "not sent")

print("broadcaster — the fix is aged again right before sending:")
fs3, clk3 = FakeSender(), Clock()


def slow_ok(frm, to, course):                    # an approval that takes 11 s
    clk3.t += 11
    return True


b3 = A._Broadcaster(sender=fs3, clock=clk3, leg_check=slow_ok,
                    fix_source=lambda: {"fix": {"lat": 43.0103,
                                                "lon": -82.0 + east(43.0103, 300),
                                                "t": clk3.t}, "course": "L1:race"})
b3.state["expires_at"] = clk3.t + 1000
tr3 = navcalc.LegTracker(WP, 1)
b3.cycle(body(), tr3, 0)
check("fix fresh at the start of the cycle, 11 s old after the approval → nothing sent",
      fs3.sent == [] and b3.state["steer_paused"] == "no position" and not b3.state["nav_live"])
check("RouteBody takes mark_idx", A.RouteBody(name="x", waypoints=[
    {"lat": 1, "lon": 1, "mark": True, "mark_idx": 2}, {"lat": 2, "lon": 2}]).waypoints[0]
      .mark_idx == 2)
fs4, clk4 = FakeSender(), Clock()
b4 = A._Broadcaster(sender=fs4, clock=clk4, leg_check=CLEAR,
                    fix_source=lambda: {"fix": {"lat": wide[0], "lon": wide[1], "t": clk4.t},
                                        "course": "L1:race", "next_mark_index": 3})
b4.state["expires_at"] = clk4.t + 1000
b4.cycle(body(waypoints=WPR), navcalc.LegTracker(WPR, 1), 0)
check("the fix feed's next_mark_index reaches the tracker (wide rounding → wp 2)",
      b4.state["nav"]["dest_index"] == 2)

print("POST /route — strict validation:")
from fastapi.testclient import TestClient  # noqa: E402

feed = {"v": None}
A.BC = A._Broadcaster(sender=FakeSender(), fix_source=lambda: feed["v"], leg_check=CLEAR)
client = TestClient(A.app)                 # not a context manager: no bus thread started
good = {"name": "C4 left", "waypoints": [{"name": "C4-01", "lat": 43, "lon": -82.0, "t": 1783000000},
                                         {"name": "C4-02", "lat": 43.1, "lon": -82.1}],
        "dest_index": 1, "course": "L1:race"}
r = client.post("/route", json=good)
check("a good route → 200, broadcasting", r.status_code == 200 and r.json()["broadcasting"])


def bad(label, mutate, raw=None):
    d = json.loads(json.dumps(good))
    if mutate:
        mutate(d)
    r = client.post("/route", content=raw or json.dumps(d),
                    headers={"Content-Type": "application/json"})
    check(f"{label} → 422", r.status_code == 422)


bad("lat 95", lambda d: d["waypoints"][0].update(lat=95))
bad("lon -181", lambda d: d["waypoints"][1].update(lon=-181))
bad("lon as a string", lambda d: d["waypoints"][1].update(lon="-82"))
bad("lat true", lambda d: d["waypoints"][1].update(lat=True))
bad("missing lon", lambda d: d["waypoints"][1].pop("lon"))
bad("NaN lat", None, raw=json.dumps(good).replace('"lat": 43,', '"lat": NaN,'))
bad("Infinity lon", None, raw=json.dumps(good).replace('"lon": -82.1', '"lon": Infinity'))
bad("one waypoint", lambda d: d.update(waypoints=d["waypoints"][:1]))
bad("251 waypoints", lambda d: d.update(waypoints=[{"lat": 43, "lon": -82}] * 251))
bad("dest_index past the end", lambda d: d.update(dest_index=2))
bad("negative dest_index", lambda d: d.update(dest_index=-1))
bad("unknown field", lambda d: d.update(steer=True))
bad("mark as a string", lambda d: d["waypoints"][0].update(mark="yes"))
bad("non-ASCII name", lambda d: d["waypoints"][0].update(name="Bouée"))
bad("name over 32 chars", lambda d: d.update(name="x" * 33))
check("a rejected route leaves the current one running", A.BC.state["broadcasting"])
_bc = A.BC


class _Stuck:
    state = {}

    def start(self, body):
        raise A.StuckWorker("Previous broadcast didn't stop. Not sent.")


A.BC = _Stuck()
r = client.post("/route", json=good)
check("old worker stuck → 503 'Previous broadcast didn't stop. Not sent.'",
      r.status_code == 503 and r.json()["detail"] == "Previous broadcast didn't stop. Not sent.")
A.BC = _bc
A.BC.start(A.RouteBody(**dict(good, course="B1:race")))
r = client.post("/stop", json={"keep_course": "B1:race"})
check("POST /stop {keep_course: this route's} → still broadcasting",
      r.status_code == 200 and r.json()["broadcasting"])
r = client.post("/stop", json={"keep_course": "A1:race"})
check("POST /stop {keep_course: another} → stopped", not r.json()["broadcasting"]
      and r.json()["ended"] == "course changed")
check("POST /stop with no body still works", client.post("/stop").status_code == 200)
r = client.post("/route", json=dict(good, waypoints=[{"lat": 43, "lon": -82}] * 250))
check("250 waypoints → accepted (packed under the 223-byte cap)", r.status_code == 200)
client.post("/stop")

print("bus thread — ISO requests:")
fs = FakeSender()
clk = Clock()
bt = A._BusThread("x", sender=fs, clock=clk, link_state=lambda: "ERROR-PASSIVE")


def req(pgn, dest=35, sa=7):
    cid = (6 << 26) | (0xEA << 16) | (dest << 8) | sa
    fs.sent.clear()
    bt.handle(cid | n2k.CAN_EFF_FLAG, pgn.to_bytes(3, "little"))
    return fs.sent


s = req(60928, dest=0xFF)
check("global request for 60928 → our claim (NAME)", [p for p, *_ in s] == [60928]
      and n2k.decode_name(s[0][1]) == n2k.NAME)
s = req(126996)
check("126996 → product info", [p for p, *_ in s] == [126996])
s = req(126464)
lists = {d[0]: [int.from_bytes(d[i:i + 3], "little") for i in range(1, len(d), 3)]
         for p, d, *_ in s}
check("126464 → transmit list + receive list",
      [p for p, *_ in s] == [126464, 126464] and 129284 in lists[0] and 59904 in lists[1])
s = req(126993)
check("126993 → heartbeat, carries error-passive", [p for p, *_ in s] == [126993]
      and s[0][1][3] & 3 == 1)
s = req(126998)
check("directed request we can't answer → NAK, sent to the requester",
      [p for p, *_ in s] == [59392] and s[0][1][0] == 1 and s[0][2] == 7
      and int.from_bytes(s[0][1][5:8], "little") == 126998)
s = req(126998, dest=0xFF)
check("global request we can't answer → silence", s == [])
s = req(126996, dest=50)
check("request for another device → silence", s == [])
check("the requester is a peer; 4 directed requests counted",
      bt.peers_recent() == [7] and bt.requests_to_us == 4)
check("answered counts kept", bt.state["answered"]["126996"] == 1 and bt.state["naks"] == 1)

print("bus thread — address-claim contest:")


def claim_from(sa, name):
    cid = (6 << 26) | (0xEE << 16) | (0xFF << 8) | sa
    fs.sent.clear()
    bt.handle(cid | n2k.CAN_EFF_FLAG, struct.pack("<Q", name))
    return fs.sent


s = claim_from(35, n2k.NAME + 1)
check("a higher NAME claims our address → we defend (claim again, keep 35)",
      fs.sa == 35 and [p for p, *_ in s] == [60928] and bt.state["conflicts"] == 1)
s = claim_from(35, 1)
check("a lower NAME claims our address → we move to 128 and claim it",
      fs.sa == 128 and bt.state["address"] == 128 and s[-1][0] == 60928 and s[-1][3] == 128)
fs.sent.clear()
conf0, sa0 = bt.state["conflicts"], fs.sa
bt.handle(0x19EE0000 | (0xFF << 8) | fs.sa | n2k.CAN_EFF_FLAG, struct.pack("<Q", 1))
check("a 126464 PGN list (DP=1, PF EE) from our address is NOT an address claim",
      bt.state["conflicts"] == conf0 and fs.sa == sa0 and fs.sent == [])
bt.handle(0x19EA0000 | (35 << 8) | 7 | n2k.CAN_EFF_FLAG, (126996).to_bytes(3, "little"))
check("a DP=1 frame with PF EA is NOT an ISO request", fs.sent == [])


def kernel_match(filters, raw):
    """SocketCAN: a frame (its can_id WITH the EFF/RTR flags) passes when
    (can_id & mask) == (filter_id & mask) for any filter."""
    for k in range(0, len(filters), 8):
        fid, mask = struct.unpack("<II", filters[k:k + 8])
        if (raw & mask) == (fid & mask):
            return True
    return False


EFF, RTR = n2k.CAN_EFF_FLAG, 0x40000000
F = A.rx_filters()
check("rx filter: ISO request 18EA2307, address claim 18EEFF07, heartbeat 1DF01107 → in",
      kernel_match(F, EFF | 0x18EA2307) and kernel_match(F, EFF | 0x18EEFF07)
      and kernel_match(F, EFF | 0x1DF01107))
check("rx filter: 126464 (19EEFF07) and other DP=1 PDU1 traffic → out",
      not kernel_match(F, EFF | 0x19EEFF07) and not kernel_match(F, EFF | 0x19EA2307))
check("rx filter: an 11-bit frame and a remote (RTR) frame → out",
      not kernel_match(F, 0x0EE) and not kernel_match(F, EFF | RTR | 0x18EEFF07))
s = claim_from(42, 5)
check("another device's claim on its own address → no reaction, it's a peer",
      s == [] and 42 in bt.peers_recent())
s = claim_from(128, 2)
check("lose again → next free address (129)", fs.sa == 129)
clk.t += A.PEER_WINDOW_S + 1
check("peers age out of the list", bt.peers_recent() == [])

print("bus thread — after a lost contest, nothing goes out from the new address before its claim:")
wire = []


class OrderSock:
    def send(self, raw):
        cid = struct.unpack("<I", raw[:4])[0] & 0x1FFFFFFF
        wire.append((n2k.pgn_of(cid), cid & 0xFF))


class RacySender(n2k.N2kSender):
    """The moment the address changes, another thread (the broadcaster) tries to send."""
    _sa = 35

    @property
    def sa(self):
        return self._sa

    @sa.setter
    def sa(self, v):
        self._sa = v
        if v != 35:
            th = threading.Thread(target=lambda: self.send(*n2k.encode_129283(1.0)))
            th.start()
            th.join(0.2)                  # old code: it gets through here, before the claim
            self.racer = th


rs = RacySender("fake")
rs._sock = OrderSock()
btr = A._BusThread("x", sender=rs, clock=Clock())
btr.handle(((6 << 26) | (0xEE << 16) | (0xFF << 8) | 35) | n2k.CAN_EFF_FLAG, struct.pack("<Q", 1))
rs.racer.join(2)
from_new = [pgn for pgn, sa in wire if sa == 128]
check(f"first frame from the new address 128 is its claim [{from_new}]",
      from_new[:1] == [60928] and 129283 in from_new)

print("startup: nothing ordinary goes out before the address claim:")
check("the service's sender starts with its claim pending",
      A.BUS.claim_pending is not None and A.BUS.claim_pending[0] == 60928)


class StartSock:
    def __init__(self):
        self.down, self.frames = True, []

    def send(self, raw):
        if self.down:
            raise OSError(100, "Network is down")
        cid = struct.unpack("<I", raw[:4])[0] & 0x1FFFFFFF
        self.frames.append(n2k.pgn_of(cid))

    def close(self):
        pass


ss = StartSock()
bus0 = n2k.N2kSender("fake", claim=n2k.encode_60928(n2k.NAME))
bus0._sock = ss
bus0.open = lambda: setattr(bus0, "_sock", ss)


class OneLoopRx:
    def recvmsg(self, n):
        raise OSError(100, "stop after one pass")

    def close(self):
        pass


bt0 = A._BusThread("x", sender=bus0, open_rx=lambda iface: OneLoopRx())
bt0.stop_evt.wait = lambda t=None: None             # no 250 ms pause in the test
try:
    bt0._loop_once()                                # claim fails (bus down), heartbeat tries
except OSError:
    pass
check("first claim fails → heartbeat refused, nothing on the wire, claim still pending",
      ss.frames == [] and bus0.claim_pending is not None)
n = bus0.send(*n2k.encode_129283(1.0))
check("a nav frame right after → refused too", n == 0 and ss.frames == [])
ss.down = False
bus0._claim_tried_at -= 1.1
bus0.send(*n2k.encode_126993())
check("bus back, a second later → the claim goes first, then the heartbeat",
      ss.frames == [60928, 126993] and bus0.claim_pending is None)

print("bus thread — /health reads the peer list while the bus thread writes it:")
errs = []


def writer():
    for k in range(200000):
        bt.handle(((6 << 26) | (0xEE << 16) | (0xFF << 8) | (k % 120 + 1)) | n2k.CAN_EFF_FLAG,
                  struct.pack("<Q", (1 << 62) + k))
        if k % 50 == 0:
            with bt.peers_lock:
                bt.peers.clear()          # churn: entries come and go


def reader():
    try:
        for _ in range(4000):
            bt.peers_recent()
    except RuntimeError as e:
        errs.append(e)


bt.sender = FakeSender()
bt.sender.sa = 200
wt = threading.Thread(target=writer)
rt = threading.Thread(target=reader)
wt.start()
rt.start()
rt.join()
wt.join()
check("no 'dictionary changed size during iteration' while reading", errs == [])

print("bus thread — restarts after a socket error:")


class FakeRx:
    def __init__(self, fail):
        self.fail, self.closed = fail, False

    def recvmsg(self, n):
        if self.fail:
            raise OSError(100, "Network is down")
        time.sleep(0.02)
        raise socket.timeout()

    def close(self):
        self.closed = True


opens = {"n": 0}


def open_rx(iface):
    opens["n"] += 1
    if opens["n"] == 1:
        raise OSError(19, "No such device")       # the mcp2515 not up yet at boot
    return FakeRx(fail=opens["n"] == 2)           # then a socket that dies mid-loop


fs = FakeSender()
bt = A._BusThread("x", sender=fs, open_rx=open_rx)
bt.backoff_s = 0.01
bt.start()
run_until(bt, lambda: opens["n"] >= 3 and bt.state["running"])
check("open failed, then recv failed → restarted, running again",
      bt.state["running"] and bt.state["restarts"] == 2 and bt.is_alive())
check("the bus loop retries a pending claim every second", getattr(fs, "retries", 0) >= 1)
check("each (re)start claims the address + sends a heartbeat",
      fs.pgns().count(60928) >= 2 and 126993 in fs.pgns())
A.RESPONDER = bt
A.can_link = lambda *a, **k: {"present": True, "up": True, "state": "ERROR-ACTIVE"}
check("/health ok while it runs", client.get("/health").json()["status"] == "ok")
bt.stop_evt.set()
bt.join(2)
bt.state["running"] = False
check("/health degraded when it is down", client.get("/health").json()["status"] == "degraded")

print("diagnostics — the CAN state from ip -details link:")
IP_JSON = json.dumps([{"ifname": "can0", "flags": ["NOARP", "UP", "LOWER_UP", "ECHO"],
                       "linkinfo": {"info_kind": "can",
                                    "info_data": {"state": "BUS-OFF", "restart_ms": 100},
                                    "info_xstats": {"restarts": 3, "bus_error": 0,
                                                    "error_warning": 1, "error_passive": 2,
                                                    "bus_off": 3}}}])


class R:
    def __init__(self, rc, out):
        self.returncode, self.stdout = rc, out


import importlib  # noqa: E402

A2 = importlib.reload(A)                          # the real can_link again
L = A2.can_link("can0", run=lambda *a, **k: R(0, IP_JSON), ttl=0)
check("state, restart-ms, bus-off and restart counts read", L["state"] == "BUS-OFF"
      and L["restart_ms"] == 100 and L["bus_off"] == 3 and L["restarts"] == 3 and L["up"])
L = A2.can_link("can0", run=lambda *a, **k: R(1, ""), ttl=0)
check("no such interface → not present", not L["present"])


def boom(*a, **k):
    raise FileNotFoundError("ip")


check("no ip binary → not present, no crash", not A2.can_link("can0", run=boom, ttl=0)["present"])

print("diagnostics — the iPad's one line:")
fs = FakeSender()
now = 1000.0
up = {"present": True, "up": True, "state": "ERROR-ACTIVE"}
fs.last_ok_at = now - 1
check("healthy → sending", A2.link_word(up, fs, True, now) == "sending")
check("bus-off → no bus", A2.link_word(dict(up, state="BUS-OFF"), fs, True, now) == "no bus")
check("interface missing → no bus", A2.link_word({"present": False}, fs, True, now) == "no bus")
check("interface down → no bus", A2.link_word(dict(up, up=False), fs, True, now) == "no bus")
check("error-passive (nobody acks us) → errors",
      A2.link_word(dict(up, state="ERROR-PASSIVE"), fs, True, now) == "errors")
fs.last_error_at = now - 5
check("a send error in the last minute → errors", A2.link_word(up, fs, True, now) == "errors")
fs.last_error_at = now - 120
check("an old send error → sending again", A2.link_word(up, fs, True, now) == "sending")
check("bus thread down → errors", A2.link_word(up, fs, False, now) == "errors")
fs7 = FakeSender()
check("nothing has ever reached the bus → no bus (not 'sending')",
      A2.link_word(up, fs7, True, now) == "no bus")
fs7.last_ok_at = now - A2.OK_WINDOW_S - 1
check("nothing sent for over 2 min → no bus", A2.link_word(up, fs7, True, now) == "no bus")

A2.can_link = lambda *a, **k: dict(up, restart_ms=100, bus_off=0)
A2.BUS.frames_sent, A2.BUS.send_errors = 12, 1
c2 = TestClient(A2.app)
st = c2.get("/status").json()["bus"]
check("/status bus: link, address, frames, errors, can state, peers",
      st["link"] in ("sending", "errors", "no bus") and st["address"] == 35
      and st["frames_sent"] == 12 and st["send_errors"] == 1
      and st["can"]["state"] == "ERROR-ACTIVE" and st["peers"] == [])
h = c2.get("/health").json()
check("/health carries the same bus block", "bus" in h and h["bus"]["can"]["restart_ms"] == 100)

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
