"""Route-deviation — is the boat sailing the frozen playbook variant's optimized track?

Lab-3 onboard executor, FIRST SLICE: the ROUTE-DEVIATION branch trigger + the iPad Strategy card.
The frozen Lab-2 bundle (`c4.playbook/v1`) carries, per strategic VARIANT, the optimizer's full
route as an absolute-time-stamped polyline — `variant.route.path = [{lat, lon, t}]`, where `t` is the
epoch ETA the plan expected the boat to reach that point. This module compares the boat's LIVE
position (+ heading/SOG) against the ACTIVE variant's frozen track and reports the perflab §5
fuzzy-adherence metrics, DETERMINISTICALLY and TIER-1 (the boat's own computer over its own GPS +
pre-loaded homework), so it is legal in-race and needs no Orin / no cloud round-trip:

  - `xte_nm`         cross-track error off the optimal polyline (signed: side = left|right of track)
  - `along_pct`      how far along the route the boat has progressed (projection onto the polyline)
  - `time_behind_s`  wall-clock behind (+) / ahead (−) of where the plan said we'd be by now, from
                     the path `t` stamps (anchored to the plan start, re-anchorable via `since`)
  - `vmc_*`          speed made good ALONG the optimal track now vs the plan's pace on this segment

FUZZY, not black/white (perflab §5): the status uses SCHMITT bands — a soft *consider* band and a
hard *commit* band with hysteresis on the drop side, so a boat hovering near a threshold doesn't
chatter between states. The result is a dashboard-tile-shaped payload (status / value / sub / why /
consider / clears / based / conf) plus the raw metrics and the active variant's `what_flips_it`, so
the iPad Strategy card renders it and the crew see WHY. This deterministic tile just measures the boat
against the pre-authored variant's track (onboard is legal in-race — the boat's own gear, not outside help).
No LLM: the deviation truth is deterministic and always available; a copilot narration layer may
reference it later through grounding-as-routing.

Loading the homework: `POST /playbook/load` persists the bundle via `datasource.save_playbook`; this
module reads it back with `datasource.active().get_playbook()`. A file fallback (`PLAYBOOK_PATH`, the
same env the copilot uses) makes "drop the signed bundle at a path" work too.
"""
import json
import math
import os
import time

from . import datasource, navigator

# geo + unit handling live in navigator — reuse them so the deviation math is byte-consistent with
# the rest of the engine (importing navigator is safe: it imports datasource, not this module).
_hav_nm = navigator._hav_nm
_bearing = navigator._bearing
_wrap180 = navigator._wrap180

# --- fuzzy Schmitt bands (perflab §5a: a consider band + a commit band, with hysteresis) --------
XTE_CONSIDER_NM = float(os.environ.get("DEV_XTE_CONSIDER_NM", "0.4"))   # drifting off the line
XTE_COMMIT_NM = float(os.environ.get("DEV_XTE_COMMIT_NM", "1.0"))      # genuinely off the track
BEHIND_CONSIDER_S = float(os.environ.get("DEV_BEHIND_CONSIDER_S", "120"))   # slipping behind plan
BEHIND_COMMIT_S = float(os.environ.get("DEV_BEHIND_COMMIT_S", "300"))
HYST = float(os.environ.get("DEV_HYST", "0.7"))       # drop-edge = enter-edge × this (no chatter)
TREND_EPS_NM = float(os.environ.get("DEV_TREND_EPS_NM", "0.05"))       # XTE change to call a trend

_BANDS = {0: "ok", 1: "watch", 2: "act"}

# Per-(route, variant) hysteresis + trend memory. A Schmitt trigger REMEMBERS — a stateless tile
# would flip-flop near a threshold. Persisting module state across polls is exactly what we want on
# a live in-race signal; `reset_state()` clears it on a race / course / variant change.
_state: dict = {}


def reset_state(key=None):
    if key is None:
        _state.clear()
    else:
        _state.pop(key, None)


def _band(value, prev, consider, commit, rel=HYST):
    """Double Schmitt: RISE to a level at its full threshold, FALL out of it only below the (lower)
    threshold × `rel`. So `consider`/`commit` are the enter edges and `×rel` the leave edges —
    hysteresis kills chatter when the metric hovers on a boundary."""
    if prev >= 2:      # currently ACT — hold until it clearly recovers
        return 2 if value >= commit * rel else (1 if value >= consider * rel else 0)
    if prev == 1:      # currently WATCH — escalate at commit, relax below consider×rel
        return 2 if value >= commit else (1 if value >= consider * rel else 0)
    return 2 if value >= commit else (1 if value >= consider else 0)   # currently OK


# --- geometry ----------------------------------------------------------------------------------

def _seg_len_nm(a, b):
    return _hav_nm(a["lat"], a["lon"], b["lat"], b["lon"])


def _project(blat, blon, path):
    """Project the boat onto the frozen route polyline. Returns the nearest-segment fix: signed
    cross-track (nm, + side), cumulative along-track distance (nm), the interpolated plan time `t`
    at that point, and the local segment bearing + the plan's along-track speed there. A flat local
    tangent plane about each segment's first point keeps the projection accurate over a leg."""
    if not path or len(path) < 2:
        return None
    cum = 0.0
    best = None
    for i in range(len(path) - 1):
        A, B = path[i], path[i + 1]
        seg = _seg_len_nm(A, B)
        # local east/north nm about A
        coslat = math.cos(math.radians(A["lat"]))
        bx = (B["lon"] - A["lon"]) * 60.0 * coslat
        by = (B["lat"] - A["lat"]) * 60.0
        px = (blon - A["lon"]) * 60.0 * coslat
        py = (blat - A["lat"]) * 60.0
        L2 = bx * bx + by * by
        s = 0.0 if L2 == 0 else max(0.0, min(1.0, (px * bx + py * by) / L2))
        projx, projy = bx * s, by * s
        perp = math.hypot(px - projx, py - projy)
        if best is None or perp < best["perp"]:
            # cross-product z of A→B × A→P : >0 = boat is LEFT of the track direction
            cross = bx * py - by * px
            ta, tb = A.get("t"), B.get("t")
            dt_h = ((tb - ta) / 3600.0) if (ta is not None and tb is not None) else None
            best = {
                "perp": perp,
                "side": "left" if cross > 0 else "right",
                "along": cum + s * seg,
                "t_here": (ta + s * (tb - ta)) if (ta is not None and tb is not None) else None,
                "seg_bearing": _bearing(A["lat"], A["lon"], B["lat"], B["lon"]),
                "seg_speed": (seg / dt_h) if (dt_h and dt_h > 0) else None,
            }
        cum += seg
    best["route_nm"] = cum
    best["t0"] = path[0].get("t")
    return best


# --- playbook / variant ------------------------------------------------------------------------

def _load_playbook():
    """The frozen bundle aboard — from the datasource (POST /playbook/load) first, else the file at
    PLAYBOOK_PATH (the same env the copilot reads), so either deploy path works."""
    try:
        blob = datasource.active().get_playbook() or {}
    except Exception:
        blob = {}
    if blob:
        return blob
    path = os.environ.get("PLAYBOOK_PATH", "").strip()
    if path and os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _pick_variant(bundle, variant):
    """The ACTIVE variant to measure against: the requested id, else the bundle's recommended start
    default, else the first — mirrors the adherence tile's default."""
    variants = bundle.get("variants") or []
    if not variants:
        return None
    if variant:
        for v in variants:
            if str(v.get("id") or v.get("name")) == str(variant):
                return v
    rec = bundle.get("recommended")
    if rec:
        for v in variants:
            if str(v.get("id")) == str(rec):
                return v
    return variants[0]


def _label(v):
    return v.get("name") or str(v.get("id") or "the plan")


# --- presentation helpers ----------------------------------------------------------------------

def _mmss(seconds):
    m, s = divmod(int(round(abs(seconds))), 60)
    return f"{m}:{s:02d}"


def _na(note, extra=None):
    out = {"available": False, "status": "na", "value": "—", "sub": note, "why": note,
           "consider": "—", "clears": "—", "based": [], "conf": "engine"}
    if extra:
        out.update(extra)
    return out


# --- the deterministic read --------------------------------------------------------------------

def get_deviation(route=None, variant=None, since=None):
    """Route-deviation tile payload for the ACTIVE playbook variant. `route` names the loaded course
    (defaults to the navigator's active route, informational here); `variant` picks which variant to
    measure against (default = the bundle's recommended); `since` re-anchors the time-behind metric
    to the actual gun epoch (default = the plan's start, so time_behind = now − planned-ETA-here)."""
    bundle = _load_playbook()
    if not bundle:
        return _na("no playbook aboard")
    v = _pick_variant(bundle, variant)
    if not v:
        return _na("playbook has no variants")
    vid = str(v.get("id") or v.get("name") or "")
    vlabel = _label(v)
    what_flips = v.get("what_flips_it") or ""
    path = ((v.get("route") or {}).get("path")) or []
    base_extra = {"variant": vid, "variant_label": vlabel, "what_flips_it": what_flips,
                  "race_id": bundle.get("race_id"), "recommended": bundle.get("recommended")}
    if len(path) < 2:
        return _na(f"variant '{vlabel}' carries no route track", base_extra)

    s = navigator._latest()
    if s.get("lat") is None or s.get("lon") is None:
        return _na("no position fix yet", base_extra)
    blat, blon = s["lat"], s["lon"]

    fix = _project(blat, blon, path)
    if fix is None:
        return _na("route track too short to measure", base_extra)

    route_nm = fix["route_nm"]
    along_nm = fix["along"]
    along_pct = round(100.0 * along_nm / route_nm) if route_nm else None
    xte_nm = round(fix["perp"], 2)
    side = fix["side"]

    # time behind: (elapsed since the anchor) − (the plan's elapsed to reach this along-point).
    now = time.time()
    t0, t_here = fix["t0"], fix["t_here"]
    time_behind = None
    if t0 is not None and t_here is not None:
        anchor = since if since is not None else t0
        boat_elapsed = now - anchor
        plan_elapsed = t_here - t0
        if boat_elapsed >= 0:          # only once the race is under way vs the anchor
            time_behind = boat_elapsed - plan_elapsed

    # VMC: boat's speed made good ALONG the optimal track vs the plan's pace on this segment.
    vmc = vmc_opt = vmc_def = None
    if s.get("sog") is not None and s.get("cog") is not None:
        vmc = round(s["sog"] * math.cos(math.radians(_wrap180(s["cog"] - fix["seg_bearing"]))), 2)
    if fix["seg_speed"] is not None:
        vmc_opt = round(fix["seg_speed"], 2)
    if vmc is not None and vmc_opt is not None:
        vmc_def = round(vmc_opt - vmc, 2)

    # --- fuzzy status (Schmitt bands with hysteresis + an XTE trend) --------------------------
    key = f"{route or navigator.active_route()}|{vid}"
    st = _state.get(key, {})
    xb = _band(xte_nm, st.get("xte_band", 0), XTE_CONSIDER_NM, XTE_COMMIT_NM)
    bb = 0
    if time_behind is not None and time_behind > 0:
        bb = _band(time_behind, st.get("behind_band", 0), BEHIND_CONSIDER_S, BEHIND_COMMIT_S)
    # XTE trend vs the last poll (converging back / drifting further out)
    last_xte = st.get("last_xte")
    trend = "steady"
    if last_xte is not None:
        if xte_nm > last_xte + TREND_EPS_NM:
            trend = "diverging"
        elif xte_nm < last_xte - TREND_EPS_NM:
            trend = "converging"
    _state[key] = {"xte_band": xb, "behind_band": bb, "last_xte": xte_nm, "at": now}

    band = max(xb, bb)
    status = _BANDS[band]
    # which signal drives the headline (XTE wins ties — it's the more actionable one)
    xte_dom = xb >= bb
    behind_txt = (_mmss(time_behind) if time_behind is not None else None)

    based = [f"playbook:{vid}", "own GPS position", "own SOG/COG"]

    # value / why / consider / clears, phrased for the crew (advisory, never imperative — perflab §5)
    if band == 0:
        value = "On the optimal track"
        sub_bits = []
        if along_pct is not None:
            sub_bits.append(f"{along_pct}% along")
        if time_behind is not None:
            sub_bits.append(("+" if time_behind >= 0 else "−") + _mmss(time_behind)
                            + (" behind" if time_behind >= 0 else " ahead"))
        why = (f"Sailing the '{vlabel}' variant's optimized track — {xte_nm} nm off the line "
               f"({side}), " + (f"{behind_txt} behind plan pace" if time_behind and time_behind > 0
                                else "on/ahead of plan pace") + ". Hold the groove.")
        consider = "Stay on the playbook line — no branch, keep sailing the plan."
        clears = "—"
    elif xte_dom:
        drift = "Off track" if band == 2 else "Drifting"
        value = f"{drift} · {xte_nm} nm {side}"
        sub = f"XTE {xte_nm} nm {side} · {trend}"
        if along_pct is not None:
            sub += f" · {along_pct}% along"
        sub_bits = [sub]
        why = (f"The boat is {xte_nm} nm to the {side} of the '{vlabel}' variant's optimal track "
               f"and {trend}." + (" This is a genuine departure from the frozen line."
                                  if band == 2 else " Still within a soft band — could be a lane/traffic detour."))
        consider = (f"You're {side} of the plan — decide: rejoin the '{vlabel}' line, or if the "
                    "breeze has genuinely changed sides, check the branch trigger below."
                    if band == 2 else
                    f"Watch the {side} drift — nudge back toward the optimal line if it's not tactical.")
        clears = "the boat converges back onto the optimal track"
    else:
        value = f"{'Behind' if band == 2 else 'Slipping'} · {behind_txt}"
        sub = f"{behind_txt} behind plan"
        if vmc_def is not None and vmc_def > 0:
            sub += f" · VMC −{vmc_def} kts"
        if along_pct is not None:
            sub += f" · {along_pct}% along"
        sub_bits = [sub]
        onliner = xte_nm <= XTE_CONSIDER_NM
        why = (f"On the '{vlabel}' line but {behind_txt} behind the plan's pace"
               + (f" — making good {vmc_def} kts less VMC than the optimizer expected here"
                  if vmc_def else "")
               + ("; you're on the optimal track, so this is boat-speed / mode, not position."
                  if onliner else "."))
        consider = ("Losing time on the optimal line — this is speed/mode, not tactics: check trim, "
                    "target boatspeed and helm vs the sea state." if onliner else
                    "Behind plan and off the line — reassess the lane and the trim together.")
        clears = "boat-speed comes back to the plan's pace"

    sub = " · ".join([b for b in (sub_bits if isinstance(sub_bits, list) else [sub_bits]) if b]) \
        if band == 0 else sub_bits[0]

    return {
        "available": True, "status": status, "value": value, "sub": sub or "—",
        "why": why, "consider": consider, "clears": clears, "based": based, "conf": "engine",
        # metrics for the Strategy card
        "variant": vid, "variant_label": vlabel, "what_flips_it": what_flips,
        "recommended": bundle.get("recommended"), "race_id": bundle.get("race_id"),
        "xte_nm": xte_nm, "xte_side": side, "xte_trend": trend,
        "along_nm": round(along_nm, 1), "route_nm": round(route_nm, 1), "along_pct": along_pct,
        "time_behind_s": (round(time_behind) if time_behind is not None else None),
        "vmc_kn": vmc, "vmc_optimal_kn": vmc_opt, "vmc_deficit_kn": vmc_def,
        "position": {"lat": round(blat, 5), "lon": round(blon, 5)},
        "headline": bundle.get("headline", ""),
    }
