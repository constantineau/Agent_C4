"""Phase 6.2 — on-demand summarizer / debrief.

Rolls up a time window of telemetry into a performance report and stores it in
`agent_summaries`. Two on-demand entry points (NO background timer, by decision):

  - make_summary()  — a short recap of the recent window (default SUMMARY_MIN).
  - make_debrief()  — a fuller window report (default DEBRIEF_MIN, or an explicit window):
                      boatspeed vs polar, wind range + shifts, heel, distance, and every
                      alert that fired in the window (from the 6.1 `alerts` debrief history).

Both compute the same metrics; they differ in default window and how the narrative is framed.
With an ANTHROPIC_API_KEY the narrative is written by Claude from the metrics; otherwise a
deterministic templated narrative is produced so the feature works with no LLM.

Caveat: aggregates are taken across ALL sources for a path (collect-everything), so a flaky
redundant sensor can nudge an average — fine for a v1 debrief; tighten to preferred-source if
it matters. Distance is SOG-integrated (avg SOG × duration), robust to interleaved position
sources.
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

from .db import pool
from . import tools

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SUMMARY_MIN = float(os.environ.get("SUMMARY_MIN", "20"))
DEBRIEF_MIN = float(os.environ.get("DEBRIEF_MIN", "120"))

_KN = 1.943844
_DEG = 57.295779513

# SI paths we aggregate (channel name -> path); converted to display units after the query.
_AGG = {
    "stw": "navigation.speedThroughWater",
    "sog": "navigation.speedOverGround",
    "tws": "environment.wind.speedTrue",
    "twa": "environment.wind.angleTrueWater",
    "heel": "navigation.attitude.roll",
    "depth": "environment.depth.belowTransducer",
}


def _circ_mean_range(degs):
    """Circular mean (deg) + oscillation span (max signed deviation - min) for wind direction."""
    if not degs:
        return None, None
    s = sum(math.sin(math.radians(d)) for d in degs)
    c = sum(math.cos(math.radians(d)) for d in degs)
    mean = math.degrees(math.atan2(s, c)) % 360
    devs = [((d - mean + 180) % 360) - 180 for d in degs]
    return round(mean, 1), round(max(devs) - min(devs), 1)


def compute_window(start, end):
    """Aggregate telemetry + alerts over [start, end]. Returns a metrics dict (available flag)."""
    dur_min = (end - start).total_seconds() / 60.0
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT path, count(*) n, avg(value) av, max(value) mx, min(value) mn, "
            "avg(abs(value)) ava, max(abs(value)) mxa "
            "FROM telemetry_raw WHERE boat_id=%s AND time BETWEEN %s AND %s "
            "AND value IS NOT NULL AND path = ANY(%s) GROUP BY path",
            (BOAT_ID, start, end, list(_AGG.values())),
        ).fetchall()
        agg = {r["path"]: r for r in rows}

        twd_rows = conn.execute(
            "SELECT value FROM telemetry_raw WHERE boat_id=%s AND time BETWEEN %s AND %s "
            "AND path='environment.wind.directionTrue' AND value IS NOT NULL ORDER BY time",
            (BOAT_ID, start, end),
        ).fetchall()

        alert_rows = conn.execute(
            "SELECT key, kind, severity, message, raised_at, cleared_at FROM alerts "
            "WHERE boat_id=%s AND raised_at <= %s AND (cleared_at IS NULL OR cleared_at >= %s) "
            "ORDER BY raised_at",
            (BOAT_ID, end, start),
        ).fetchall()

    total_samples = sum(r["n"] for r in agg.values())
    if total_samples == 0:
        return {"available": False, "window_start": start, "window_end": end,
                "note": "no telemetry in window"}

    def av(ch, conv):
        r = agg.get(_AGG[ch])
        return round(conv(r["av"]), 2) if r and r["av"] is not None else None

    def mx(ch, conv, absolute=False):
        r = agg.get(_AGG[ch])
        if not r:
            return None
        v = r["mxa"] if absolute else r["mx"]
        return round(conv(v), 2) if v is not None else None

    def mn(ch, conv):
        r = agg.get(_AGG[ch])
        return round(conv(r["mn"]), 2) if r and r["mn"] is not None else None

    avg_stw = av("stw", lambda x: x * _KN)
    avg_sog = av("sog", lambda x: x * _KN)
    mean_tws = av("tws", lambda x: x * _KN)
    twd_mean, twd_osc = _circ_mean_range([float(r["value"]) * _DEG % 360 for r in twd_rows])

    # mean |TWA| (deg) for the polar lookup
    twa_r = agg.get(_AGG["twa"])
    mean_abs_twa = round(twa_r["ava"] * _DEG, 1) if twa_r and twa_r["ava"] is not None else None

    polar_pct = None
    target_stw = None
    if avg_stw and mean_tws is not None and mean_abs_twa is not None:
        p = tools.get_polar_target(mean_tws, mean_abs_twa)
        target_stw = p.get("target_stw")
        if target_stw:
            polar_pct = round(100 * avg_stw / target_stw)

    distance_nm = round(avg_sog * dur_min / 60.0, 2) if avg_sog else None

    alerts = []
    sev_counts = {"danger": 0, "warn": 0, "info": 0}
    for a in alert_rows:
        sev_counts[a["severity"]] = sev_counts.get(a["severity"], 0) + 1
        alerts.append({"kind": a["kind"], "severity": a["severity"], "message": a["message"],
                       "raised_at": a["raised_at"].isoformat() if a["raised_at"] else None,
                       "cleared": a["cleared_at"] is not None})

    return {
        "available": True,
        "window_start": start, "window_end": end,
        "duration_min": round(dur_min, 1), "samples": total_samples,
        "boatspeed": {"avg_stw_kn": avg_stw, "max_stw_kn": mx("stw", lambda x: x * _KN),
                      "avg_sog_kn": avg_sog, "distance_nm": distance_nm},
        "polar": {"mean_tws_kn": mean_tws, "mean_abs_twa_deg": mean_abs_twa,
                  "target_stw_kn": target_stw, "percent_of_polar": polar_pct,
                  "note": "approx: avg boatspeed vs target at mean conditions"},
        "wind": {"tws_min_kn": mn("tws", lambda x: x * _KN), "tws_mean_kn": mean_tws,
                 "tws_max_kn": mx("tws", lambda x: x * _KN),
                 "twd_mean_deg": twd_mean, "twd_oscillation_deg": twd_osc},
        "heel": {"avg_abs_deg": av("heel", lambda x: abs(x) * _DEG),
                 "max_abs_deg": mx("heel", lambda x: x * _DEG, absolute=True)},
        "depth": {"min_m": mn("depth", lambda x: x)},
        "alerts": {"count": len(alerts), "by_severity": sev_counts, "items": alerts},
    }


def _template_narrative(m, mode):
    """Deterministic narrative when there's no LLM (or it errors)."""
    bs, pol, w = m["boatspeed"], m["polar"], m["wind"]
    bits = [f"{mode.capitalize()} — {m['duration_min']:.0f} min."]
    if bs.get("avg_stw_kn") is not None:
        line = f"Avg boatspeed {bs['avg_stw_kn']} kts (max {bs['max_stw_kn']})"
        if pol.get("percent_of_polar") is not None:
            line += f", ~{pol['percent_of_polar']}% of the {pol['target_stw_kn']} kts polar target"
        bits.append(line + f"; {bs.get('distance_nm', '?')} nm sailed.")
    if w.get("tws_mean_kn") is not None:
        bits.append(f"Breeze {w['tws_min_kn']}–{w['tws_max_kn']} kts (mean {w['tws_mean_kn']}), "
                    f"TWD ~{w['twd_mean_deg']}° oscillating ±{round((w['twd_oscillation_deg'] or 0)/2)}°.")
    if m["heel"].get("avg_abs_deg") is not None:
        bits.append(f"Heel avg {m['heel']['avg_abs_deg']}°, peak {m['heel']['max_abs_deg']}°.")
    al = m["alerts"]
    if al["count"]:
        sv = al["by_severity"]
        bits.append(f"{al['count']} alert(s) fired ({sv.get('danger',0)} danger / "
                    f"{sv.get('warn',0)} warn): "
                    + "; ".join(a["message"] for a in al["items"][:4]) + ".")
    else:
        bits.append("No alerts fired.")
    return " ".join(bits)


def _llm_narrative(m, mode):
    import anthropic
    client = anthropic.Anthropic(api_key=API_KEY)
    instr = ("You are the SR33 race navigator writing a crew " + mode + ". Be concise and "
             "concrete, use the numbers given, and surface the 2-3 things that matter most "
             "(speed vs polar, wind pattern/shifts, any safety alerts, a takeaway). "
             + ("A debrief is 4-6 sentences with a clear takeaway. "
                if mode == "debrief" else "A summary is 2-3 sentences. ")
             + "Don't invent data beyond what's provided.")
    resp = client.messages.create(
        model=MODEL, max_tokens=600,
        system=instr,
        messages=[{"role": "user", "content": "Window metrics:\n" + json.dumps(m, default=str)}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _narrative(m, mode):
    if API_KEY:
        try:
            return _llm_narrative(m, mode)
        except Exception as exc:
            return f"{_template_narrative(m, mode)}  [LLM unavailable: {exc}]"
    return _template_narrative(m, mode)


def _store(start, end, text):
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO agent_summaries (boat_id, window_start, window_end, summary) "
            "VALUES (%s, %s, %s, %s) RETURNING id, time",
            (BOAT_ID, start, end, text),
        ).fetchone()
        conn.commit()
    return row


def _make(mode, minutes, start, end):
    now = datetime.now(timezone.utc)
    if end is None:
        end = now
    if start is None:
        start = end - timedelta(minutes=minutes)
    m = compute_window(start, end)
    if not m.get("available"):
        return {"available": False, "window_start": start.isoformat(),
                "window_end": end.isoformat(), "note": m.get("note")}
    text = _narrative(m, mode)
    rec = _store(start, end, text)
    return {"available": True, "id": rec["id"], "stored_at": rec["time"].isoformat(),
            "mode": mode, "window_start": start.isoformat(), "window_end": end.isoformat(),
            "summary": text, "metrics": m}


def make_summary(minutes: float = None, start=None, end=None):
    return _make("summary", minutes if minutes is not None else SUMMARY_MIN, start, end)


def make_debrief(minutes: float = None, start=None, end=None):
    return _make("debrief", minutes if minutes is not None else DEBRIEF_MIN, start, end)


def get_summaries(limit: int = 5):
    """Most recent stored summaries/debriefs (newest first) for the agent to recall."""
    limit = max(1, min(int(limit), 20))
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, time, window_start, window_end, summary FROM agent_summaries "
            "WHERE boat_id=%s ORDER BY time DESC LIMIT %s", (BOAT_ID, limit),
        ).fetchall()
    return {"count": len(rows), "summaries": [
        {"id": r["id"], "stored_at": r["time"].isoformat(),
         "window_start": r["window_start"].isoformat() if r["window_start"] else None,
         "window_end": r["window_end"].isoformat() if r["window_end"] else None,
         "summary": r["summary"]} for r in rows]}
