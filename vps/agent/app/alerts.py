"""Phase 6.1 — alerting.

Conservative, debounced safety/performance alerts, evaluated on a background loop (see
agent/main.py). Each rule reports zero or more *conditions* that are true right now; a
condition must hold continuously for its debounce window before it RAISES (so a single noisy
sample never fires), and it CLEARS as soon as the condition goes false — raise slow, clear
fast. Active alerts and their history live in the `alerts` table: `cleared_at IS NULL` is the
live set, retained cleared rows are the debrief record.

`evaluate()` diffs the firing set against the active rows, writes the changes, and returns the
new / updated / cleared deltas so the caller can push them over the WebSocket. It is meant to
be run from a threadpool (synchronous DB pool) every ALERT_EVAL_SECONDS.

Design notes / caveats:
- Thresholds are env-tunable first-cut values; the exit test is an acceptable false-positive
  rate over two practice sails, so expect to tune ALERT_* against real archives.
- Safety rules (AIS, depth, stale data) always matter; performance/tactical rules (polar
  deficit, wind shift) carry the usual RRS 41 caveat in a race — the agent prompt says so.
- Debounce timing is kept in-process (`_pending`, monotonic); after a restart a condition
  re-accumulates over one window before raising, which is fine. The DB table is the durable
  state + history, not the sub-window debounce clock.
"""
import os
import time

from .db import pool
from . import tools

BOAT_ID = os.environ.get("BOAT_ID", "sr33")
EVAL_SECONDS = int(os.environ.get("ALERT_EVAL_SECONDS", "15"))

# --- thresholds (conservative; tune on real sails) -------------------------------------
AIS_CPA_NM = float(os.environ.get("ALERT_AIS_CPA_NM", "1.0"))      # closing CPA inside this → guard
AIS_TCPA_MIN = float(os.environ.get("ALERT_AIS_TCPA_MIN", "20"))   # …and CPA reached within this
POLAR_DEFICIT = float(os.environ.get("ALERT_POLAR_DEFICIT", "0.12"))  # boatspeed >12% under target
STALE_S = float(os.environ.get("ALERT_STALE_S", "60"))            # freshest source older than this
DEPTH_WARN_M = float(os.environ.get("ALERT_DEPTH_WARN_M", "8"))
DEPTH_DANGER_M = float(os.environ.get("ALERT_DEPTH_DANGER_M", "4"))
SHIFT_DEG = float(os.environ.get("ALERT_SHIFT_DEG", "12"))         # persistent shift magnitude

# Per-rule sustain window (seconds) the condition must hold before it raises.
DEBOUNCE_S = {
    "ais": float(os.environ.get("ALERT_DEBOUNCE_AIS", "30")),
    "wind_shift": float(os.environ.get("ALERT_DEBOUNCE_SHIFT", "90")),
    "polar_deficit": float(os.environ.get("ALERT_DEBOUNCE_POLAR", "180")),
    "stale_telemetry": float(os.environ.get("ALERT_DEBOUNCE_STALE", "30")),
    "depth_shoaling": float(os.environ.get("ALERT_DEBOUNCE_DEPTH", "10")),
    "fatigue": float(os.environ.get("ALERT_DEBOUNCE_FATIGUE", "45")),
}
_DEFAULT_DEBOUNCE = 30.0
_SEV_ORDER = {"danger": 0, "warn": 1, "info": 2}

# key -> monotonic time the condition was first seen (continuously) true
_pending = {}


# --- rules: build the set of conditions firing right now --------------------------------
def _conditions():
    out = []

    # 1) AIS collision guard — a closing target with a small CPA reached soon.
    try:
        ais = tools.get_ais_targets()
        if ais.get("own_fix"):
            for t in ais.get("targets", []):
                cpa, tcpa = t.get("cpa_nm"), t.get("tcpa_min")
                if t.get("closing") and cpa is not None and tcpa is not None \
                        and cpa <= AIS_CPA_NM and 0 < tcpa <= AIS_TCPA_MIN:
                    sev = "danger" if (cpa <= AIS_CPA_NM * 0.4 or tcpa <= 6) else "warn"
                    who = t.get("name") or t["mmsi"]
                    out.append({"key": f"ais:{t['mmsi']}", "kind": "ais", "severity": sev,
                                "message": f"Closing AIS {who}: CPA {cpa} nm in {tcpa} min, "
                                           f"bearing {t.get('bearing')}°."})
    except Exception:
        pass

    # 2/3/4) strip-derived: stale telemetry, depth, polar deficit.
    try:
        s = tools.get_strip()
        if not s.get("available"):
            out.append({"key": "stale_telemetry", "kind": "stale_telemetry", "severity": "danger",
                        "message": "No live telemetry — link or sensors silent."})
        else:
            age = s.get("data_age_seconds")
            if age is not None and age > STALE_S:
                sev = "danger" if age > STALE_S * 3 else "warn"
                out.append({"key": "stale_telemetry", "kind": "stale_telemetry", "severity": sev,
                            "message": f"Telemetry stale — freshest source {round(age)} s old."})

            depth = s.get("depth")
            if depth is not None:
                if depth <= DEPTH_DANGER_M:
                    out.append({"key": "depth_shoaling", "kind": "depth_shoaling",
                                "severity": "danger", "message": f"Shallow water — depth {depth} m."})
                elif depth <= DEPTH_WARN_M:
                    out.append({"key": "depth_shoaling", "kind": "depth_shoaling",
                                "severity": "warn", "message": f"Shoaling — depth {depth} m."})

            stw, tws, twa = s.get("stw"), s.get("tws"), s.get("twa")
            if stw and tws is not None and twa is not None:
                p = tools.get_polar_target(tws, twa)
                tgt = p.get("target_stw")
                if tgt and stw < tgt * (1 - POLAR_DEFICIT):
                    pct = round(100 * stw / tgt)
                    out.append({"key": "polar_deficit", "kind": "polar_deficit", "severity": "warn",
                                "message": f"Boatspeed {stw} kn — {pct}% of {tgt} kn target "
                                           f"({round(twa)}° TWA / {round(tws)} kn)."})
    except Exception:
        pass

    # 5) Persistent wind shift (tactical).
    try:
        tac = tools.get_tactics()
        if tac.get("available"):
            w = tac.get("wind", {})
            if w.get("persistent") and abs(w.get("shift_deg", 0)) >= SHIFT_DEG:
                out.append({"key": "wind_shift", "kind": "wind_shift", "severity": "warn",
                            "message": f"Persistent {w.get('trend')} shift "
                                       f"{abs(round(w['shift_deg']))}° — {tac.get('favored_side')} "
                                       f"side favored."})
    except Exception:
        pass

    # 6) Helm fatigue — only the strongest level.
    try:
        f = tools.get_fatigue()
        if f.get("available") and f.get("level") == "rotate_now":
            out.append({"key": "fatigue", "kind": "fatigue", "severity": "warn",
                        "message": f"Helm fatigue {round(f['index'])} — rotate now. "
                                   f"{f.get('recommendation', '')}".strip()})
    except Exception:
        pass

    return out


# --- state helpers ---------------------------------------------------------------------
def _active_rows():
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, key, kind, severity, message, "
            "extract(epoch FROM raised_at) AS raised_epoch "
            "FROM alerts WHERE boat_id=%s AND cleared_at IS NULL ORDER BY raised_at",
            (BOAT_ID,),
        ).fetchall()
    return {r["key"]: r for r in rows}


def evaluate():
    """Run the rules, persist raises/clears, return the deltas since the last tick.

    Returns a list of {event: 'new'|'updated'|'cleared', alert: {...}}."""
    now_m = time.monotonic()
    firing = {c["key"]: c for c in _conditions()}

    # advance debounce timers: drop keys no longer firing, start/keep timers for firing ones
    for k in list(_pending):
        if k not in firing:
            _pending.pop(k, None)
    raised_now = {}
    for k, c in firing.items():
        _pending.setdefault(k, now_m)
        if now_m - _pending[k] >= DEBOUNCE_S.get(c["kind"], _DEFAULT_DEBOUNCE):
            raised_now[k] = c

    active = _active_rows()
    changes = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for k, c in raised_now.items():
                if k not in active:
                    cur.execute(
                        "INSERT INTO alerts (boat_id, key, kind, severity, message, raised_at, "
                        "updated_at) VALUES (%s, %s, %s, %s, %s, now(), now())",
                        (BOAT_ID, k, c["kind"], c["severity"], c["message"]),
                    )
                    changes.append({"event": "new", "alert": dict(c)})
                else:
                    a = active[k]
                    if a["severity"] != c["severity"] or a["message"] != c["message"]:
                        cur.execute(
                            "UPDATE alerts SET severity=%s, message=%s, updated_at=now() WHERE id=%s",
                            (c["severity"], c["message"], a["id"]),
                        )
                        changes.append({"event": "updated", "alert": dict(c)})
            # clear active alerts whose condition is entirely gone (not just inside debounce)
            for k, a in active.items():
                if k not in firing:
                    cur.execute("UPDATE alerts SET cleared_at=now(), updated_at=now() WHERE id=%s",
                                (a["id"],))
                    changes.append({"event": "cleared", "alert": {
                        "key": k, "kind": a["kind"], "severity": a["severity"],
                        "message": a["message"]}})
        conn.commit()
    return changes


def active_alerts():
    """Currently-active alerts, most severe first (then oldest first)."""
    rows = _active_rows()
    items = []
    for r in rows.values():
        items.append({"key": r["key"], "kind": r["kind"], "severity": r["severity"],
                      "message": r["message"],
                      "age_s": round(time.time() - r["raised_epoch"]) if r.get("raised_epoch") else None})
    items.sort(key=lambda a: (_SEV_ORDER.get(a["severity"], 3), a.get("age_s") or 0))
    return items


def get_alerts():
    a = active_alerts()
    return {"count": len(a), "alerts": a}
