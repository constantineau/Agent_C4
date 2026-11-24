"""Logged channels over a recorded window, averaged into even time steps (#374).

The Lab debrief's strip chart plots BSP, TWS, TWD, TWA, heel and friends against time and takes
mean/SD between markers. `/racelog/track` carries only what the debrief scorer needs on each
position fix (SOG/COG/TWS/TWA/STW), so heel, heading and TWD were not reachable from the Lab.

The database averages each second (one row per second, path and source), so a 7 h race is tens
of thousands of rows per path instead of millions of raw readings. Within each second the
best-ranked source wins (the same per-second rule `/racelog/track` uses: an unranked source is
rank 999, never dropped); the chosen seconds are then averaged into the step here.
AIS-bearing sources are filtered out in SQL by the caller.

Angles are averaged as unit vectors. TWA is signed (-180..180, negative = port), the others are
0..360. TWD is the instrument's own `directionTrue` where the bus has one, else heading + TWA.
"""
import math

# channel -> (Signal K path, scale to display units, kind)
#   kind: "lin" = plain mean, "ang" = 0..360 circular mean, "sang" = signed -180..180 circular mean
KN = 1.943844
DEG = 57.29577951308232
CHANNELS = {
    "bsp": ("navigation.speedThroughWater", KN, "lin"),
    "sog": ("navigation.speedOverGround", KN, "lin"),
    "cog": ("navigation.courseOverGroundTrue", DEG, "ang"),
    "hdg": ("navigation.headingTrue", DEG, "ang"),
    "tws": ("environment.wind.speedTrue", KN, "lin"),
    "twa": ("environment.wind.angleTrueWater", DEG, "sang"),
    "twd": ("environment.wind.directionTrue", DEG, "ang"),
    "aws": ("environment.wind.speedApparent", KN, "lin"),
    "awa": ("environment.wind.angleApparent", DEG, "sang"),
    "heel": ("navigation.attitude.roll", DEG, "lin"),
}
PATHS = tuple(p for p, _s, _k in CHANNELS.values())
_BY_PATH = {p: (ch, s, k) for ch, (p, s, k) in CHANNELS.items()}


def pick_step(start, end, max_points=4000, step_s=None):
    """Seconds per step: the caller's, else the smallest whole second that keeps the window under
    `max_points` steps. Never below 1 s (the log is bucketed per second everywhere else)."""
    if step_s:
        return max(1, int(round(float(step_s))))
    span = max(1.0, float(end) - float(start))
    return max(1, int(math.ceil(span / max(1, int(max_points)))))


def assemble(rows, rank, start, step):
    """Per-second DB rows -> time-ascending samples, one per `step` seconds from `start`.

    `rows`: dicts {sec (whole epoch second), path, source, m (mean of value), s (mean sin),
    c (mean cos)}. `rank(path, source)` -> int or None (None = unranked = 999).
    The source is chosen PER SECOND (best rank reporting in that second), then the chosen
    seconds are averaged into the step: a lead that reports once in a 7 s step does not throw
    away the backup's other 6 s.
    Returns [{"t": step start, "bsp": .., "tws": .., ...}] with None for a channel missing in that step."""
    best = {}           # (sec, path) -> (rank, row)
    for r in rows:
        if r.get("sec") is None or r.get("path") not in _BY_PATH:
            continue
        rk = rank(r["path"], r.get("source"))
        rk = 999 if rk is None else rk
        key = (float(r["sec"]), r["path"])
        if key in best and best[key][0] <= rk:
            continue
        best[key] = (rk, r)
    start, step = float(start), max(1, int(step))
    acc = {}            # (b, path) -> [sum m, n m, sum s, sum c, n sc]
    for (sec, path), (_rk, r) in best.items():
        b = start + math.floor((sec - start) / step) * step
        a = acc.setdefault((b, path), [0.0, 0, 0.0, 0.0, 0])
        if r.get("m") is not None:
            a[0] += float(r["m"])
            a[1] += 1
        if r.get("s") is not None and r.get("c") is not None:
            a[2] += float(r["s"])
            a[3] += float(r["c"])
            a[4] += 1
    steps = {}
    for (b, path), a in acc.items():
        ch, scale, kind = _BY_PATH[path]
        row = {"m": a[0] / a[1] if a[1] else None,
               "s": a[2] / a[4] if a[4] else None, "c": a[3] / a[4] if a[4] else None}
        v = _value(row, scale, kind)
        if v is not None:
            steps.setdefault(b, {})[ch] = v
    out = []
    for b in sorted(steps):
        d = steps[b]
        if d.get("twd") is None and d.get("hdg") is not None and d.get("twa") is not None:
            d["twd"] = round((d["hdg"] + d["twa"]) % 360.0, 1)
        out.append({"t": b, **{ch: d.get(ch) for ch in CHANNELS}})
    return out


def _value(r, scale, kind):
    if kind == "lin":
        return None if r.get("m") is None else round(float(r["m"]) * scale, 2)
    if r.get("s") is None or r.get("c") is None:
        return None
    s, c = float(r["s"]), float(r["c"])
    if abs(s) < 1e-9 and abs(c) < 1e-9:
        return None                                  # opposite readings cancel: no direction
    a = math.degrees(math.atan2(s, c))               # -180..180
    if kind == "ang":
        a %= 360.0
    return round(a, 1)


# one row per (second, path, source): the source is picked per second in `assemble`
SQL = ("SELECT floor(extract(epoch FROM time))::float8 AS sec, path, source, "
       "avg(value) AS m, avg(sin(value)) AS s, avg(cos(value)) AS c "
       "FROM telemetry_raw WHERE boat_id = %s AND path = ANY(%s) "
       "AND time BETWEEN %s AND %s AND value IS NOT NULL ")


def query(conn, boat_id, start, end, ais_sources):
    """Run the bucketed read. `ais_sources`: sources to leave out (other vessels' fixes)."""
    from datetime import datetime, timezone
    t0 = datetime.fromtimestamp(float(start), tz=timezone.utc)
    t1 = datetime.fromtimestamp(float(end), tz=timezone.utc)
    args = [boat_id, list(PATHS), t0, t1]
    sql = SQL
    if ais_sources:
        sql += "AND NOT (source = ANY(%s)) "
        args.append(sorted(ais_sources))
    sql += "GROUP BY 1, 2, 3 ORDER BY 1"
    return conn.execute(sql, tuple(args)).fetchall()
