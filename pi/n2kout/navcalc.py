"""Live leg navigation for the plotter: which waypoint is active, and the XTE / distance /
bearing / ETA to it, recomputed from the boat's latest position on every broadcast cycle.

Pure (no bus, no clock): `LegTracker.update(lat, lon, t)` takes one fix and returns the numbers
the 129283/129284 frames carry. n2kout calls it once a second with a fresh fix and sends
nothing live when there is none.

Waypoint advance — the two rules a chartplotter uses:
  * ARRIVAL: the boat is within ARRIVE_M (0.05 nm, the same radius gpsout uses) of the active
    waypoint;
  * BISECTOR: the boat has crossed the line through the active waypoint that bisects the turn
    from the incoming leg to the outgoing one. A boat that rounds a waypoint wide (most of them:
    a route vertex is a tack or gybe point, not a buoy) still moves on, and it moves on at the
    turn, not before. When the two legs nearly reverse (a hairpin, e.g. a windward mark on an
    out-and-back route) the bisector is ill-defined, so the line perpendicular to the incoming
    leg is used: crossing it means the boat is past the mark along the leg it came up.
A waypoint that is a COURSE MARK (`mark: true` from the engine) advances on the arrival radius
only: crossing its bisector isn't rounding it. Route-only points (tacks, gybes, bends) use both.
The index only ever moves forward. The last waypoint is never passed: arriving there sets the
arrival flag and the leg stays on it until the route is cleared or expires.

OBSTACLES: n2kout holds no land, islands or zones, so every NEW leg (boat → the next waypoint) is
approved by the engine before the advance (`approve(boat, index)` → True / False / None for no
answer), with the same checks gpsout makes at send time. Not approved → the tracker does not
advance and reports `blocked`: the caller stops the live nav data (as for a stale fix) and the
crew is told to re-route. A refused check is retried every RETRY_S. The CURRENT leg is re-checked
every RECHECK_S too (the boat may have sailed off it into a hazard's shadow).

XTE is the signed great-circle distance from the leg origin→destination line: positive = boat
RIGHT of the track (steer left), the Signal K convention signalk-to-nmea2000 puts in 129283. The
first leg (destination = waypoint 0, before the start) has no previous waypoint, so its origin
is the boat's first fix on that leg — what a plotter does for a "Go To".

ETA comes from the measured closing rate on the active waypoint (how fast the distance to it
actually fell over the last ETA_WINDOW_S), not a single-sample speed projection; n/a until
there's ETA_MIN_SPAN_S of history on this leg or when the boat isn't closing.
"""
from __future__ import annotations

import math
from collections import deque

R_M = 6371008.8
NM_M = 1852.0
ARRIVE_M = 0.05 * NM_M
ETA_WINDOW_S = 120.0
ETA_MIN_SPAN_S = 20.0
MIN_CLOSING_MPS = 0.25          # ~0.5 kn: slower than that, an ETA is noise
RECHECK_S = 30.0                # the current leg's obstacle re-check
RETRY_S = 5.0                   # a refused / unanswered check is asked again this often


def dist_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1, lon1, lat2, lon2):
    """Initial great-circle bearing, degrees true [0, 360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def xte_m(a, b, p):
    """Signed cross-track distance of p from the great circle a→b; + = right of track."""
    if dist_m(a[0], a[1], b[0], b[1]) < 1.0:
        return None
    d13 = dist_m(a[0], a[1], p[0], p[1]) / R_M
    t13 = math.radians(bearing_deg(a[0], a[1], p[0], p[1]))
    t12 = math.radians(bearing_deg(a[0], a[1], b[0], b[1]))
    return math.asin(math.sin(d13) * math.sin(t13 - t12)) * R_M


def _enu(origin, p):
    """Local flat metres (east, north) of p around origin — fine over a leg's few miles."""
    k = math.cos(math.radians(origin[0]))
    return (math.radians(p[1] - origin[1]) * R_M * k, math.radians(p[0] - origin[0]) * R_M)


def _unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-9 else None


def passed(a, b, c, p):
    """Has p crossed the turn line at waypoint b (incoming a→b, outgoing b→c)? With no c (last
    waypoint) the line is the perpendicular to a→b. None for a degenerate leg."""
    u1 = _unit(_enu(b, a))
    if u1 is None:
        return False
    u1 = (-u1[0], -u1[1])                       # direction of travel a→b
    n = u1
    if c is not None:
        u2 = _unit(_enu(b, c))
        if u2 is not None:
            s = (u1[0] + u2[0], u1[1] + u2[1])
            if math.hypot(*s) > 0.3:            # not a hairpin: the bisector's normal
                n = _unit(s)
    q = _enu(b, p)
    return q[0] * n[0] + q[1] * n[1] > 0


class LegTracker:
    def __init__(self, wpts, dest_index: int, arrive_m: float = ARRIVE_M):
        self.pts = [(float(w["lat"]), float(w["lon"])) for w in wpts]
        self.marks = {i for i, w in enumerate(wpts) if w.get("mark")}
        self.last = len(self.pts) - 1
        self.di = max(0, min(self.last, int(dest_index)))
        self.arrive_m = arrive_m
        self.origin = self.pts[self.di - 1] if self.di > 0 else None
        self.hist = deque()
        self.advances = 0
        self.blocked = False
        self.checked_at = None          # the send-time check (gpsout) covers the first leg
        self.retry_at = 0.0

    def _origin(self, p):
        if self.origin is None:                  # first leg to waypoint 0: from the first fix
            self.origin = p
        return self.origin

    def _due(self, p):
        """Has the boat attained the active waypoint (arrival radius; or, for a route-only
        point, its turn line)?"""
        b = self.pts[self.di]
        if dist_m(p[0], p[1], b[0], b[1]) <= self.arrive_m:
            return True
        if self.di in self.marks:
            return False
        return passed(self._origin(p), b, self.pts[self.di + 1], p)

    def _ask(self, approve, p, idx, t):
        ok = approve(p, idx) is True
        self.blocked = not ok
        if ok:
            self.checked_at = t
        else:
            self.retry_at = t + RETRY_S
        return ok

    def _advance(self, p, t, approve):
        """→ True when an advance is due but not approved (blocked)."""
        while self.di < self.last and self._due(p):
            if approve is not None:
                if self.blocked and t < self.retry_at:
                    return True
                if not self._ask(approve, p, self.di + 1, t):
                    return True
            self.di += 1
            self.origin = self.pts[self.di - 1]
            self.hist.clear()
            self.advances += 1
        return False

    def update(self, lat: float, lon: float, t: float, approve=None) -> dict:
        p = (float(lat), float(lon))
        self._origin(p)
        if self.checked_at is None:
            self.checked_at = t
        pending = self._advance(p, t, approve)
        if approve is not None and not pending:
            if self.blocked:
                if t >= self.retry_at:
                    self._ask(approve, p, self.di, t)
            elif t - self.checked_at >= RECHECK_S:
                self._ask(approve, p, self.di, t)
        a, b = self.origin, self.pts[self.di]
        dtw = dist_m(p[0], p[1], b[0], b[1])
        final = self.di == self.last
        arrived = final and dtw <= self.arrive_m
        perp = final and passed(a, b, None, p)
        self.hist.append((t, dtw))
        while self.hist and t - self.hist[0][0] > ETA_WINDOW_S:
            self.hist.popleft()
        closing = eta = None
        t0, d0 = self.hist[0]
        if t - t0 >= ETA_MIN_SPAN_S:
            closing = (d0 - dtw) / (t - t0)
            if closing >= MIN_CLOSING_MPS:
                eta = t + dtw / closing
        return {"dest_index": self.di, "origin_index": max(0, self.di - 1),
                "dest": b, "dtw_m": dtw, "brg_deg": bearing_deg(p[0], p[1], b[0], b[1]),
                "brg_leg_deg": (bearing_deg(a[0], a[1], b[0], b[1])
                                if dist_m(a[0], a[1], b[0], b[1]) >= 1.0 else None),
                "xte_m": xte_m(a, b, p), "arrived": arrived, "perp_crossed": perp,
                "closing_mps": closing, "eta_epoch": eta, "blocked": self.blocked}
