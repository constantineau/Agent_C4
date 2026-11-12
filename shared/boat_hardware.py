"""The boat's hardware (#313): sensors, house bank, Orin, plotter, from its profile's `hardware` block.

Every boat has its own kit. Nothing here is a boat's name: the facts come from the profile, and a
profile that says nothing gets a generic default that is SAID, never another boat's setup.

    "hardware": {
      "source_priority": {"heel": ["orca", "24xd"], ...},   # channel -> device matchers, best first
      "battery": {"nominal_v": 12, "chemistry": "lead_acid"},
      "orin": true,                                         # an Orin copilot on this boat
      "plotter": {"model": "GPSMAP 943", "max_route_waypoints": 250}   # or false: no plotter
    }

  * `source_priority`: which device leads each channel (shared/source_policy.py). The matchers are
    substrings of the device identity (shared/n2k_sources.py). Absent → no priority: every channel
    takes the freshest source, and sensor health says so.
  * `battery`: the bank's nominal voltage and chemistry. The bank watch's lines scale from them
    (`battery_lines`). Absent → 12 V lead-acid, and the bank tile says it was assumed.
  * `orin`: false on a Pi-only boat. Deploy then skips the copilot step ("No Orin on this boat.")
    instead of failing it on every push. Absent → an Orin (the standard kit).
  * `plotter`: the chartplotter a route is pushed to. `max_route_waypoints` caps a pushed route.
    false → no plotter: the engine never pushes a route. Absent → a plotter of unknown model.
"""
from __future__ import annotations

import math

NO_ORIN = "No Orin on this boat."
NO_PLOTTER = "No plotter on this boat. Not sent."
NO_PRIORITY = "No sensor priority set for this boat. Each reading uses the freshest source."

CHEMISTRIES = ("lead_acid", "agm", "gel", "lithium")
DEFAULT_BATTERY = {"nominal_v": 12.0, "chemistry": "lead_acid"}
# The NMEA 2000 route list a plotter accepts in one route (n2kout packs up to this many).
PROTOCOL_MAX_WAYPOINTS = 250

# The bank watch's lines for a 12 V bank, by chemistry (app/power.py explains each). They scale
# with the bank's nominal voltage (24 V = twice the cells). Lead-acid is the line set the bank
# watch has always used (measured on the reference boat's record, 2026-09-09). Lithium (LiFePO4)
# sits flat near 13 V until it is nearly empty, then falls fast: its drain warn arms only below
# that plateau and its floor is 3.0 V a cell. FIRST-CUT lithium figures: confirm on a real bank.
_LINES_12V = {
    "lead_acid": {"floor_v": 11.0, "danger_margin_v": 0.3, "drain_level_v": 11.5,
                  "drain_warn_v_per_h": 0.15, "charge_v_per_h": 0.10},
    "lithium": {"floor_v": 12.0, "danger_margin_v": 0.4, "drain_level_v": 12.9,
                "drain_warn_v_per_h": 0.15, "charge_v_per_h": 0.10},
}
_CURVE = {"lead_acid": "lead_acid", "agm": "lead_acid", "gel": "lead_acid", "lithium": "lithium"}


def _num(x):
    try:
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    except OverflowError:
        return False


def block(profile) -> dict:
    """The profile's `hardware` block, or {} (no profile, or none set)."""
    hw = (profile or {}).get("hardware") if isinstance(profile, dict) else None
    return hw if isinstance(hw, dict) else {}


def source_priority(profile):
    """channel -> [matchers] from the profile, lowercased, or None when the boat sets none."""
    sp = block(profile).get("source_priority")
    if not isinstance(sp, dict) or not sp:
        return None
    out = {}
    for ch, ms in sp.items():
        if isinstance(ch, str) and isinstance(ms, list):
            ms = [m.strip().lower() for m in ms if isinstance(m, str) and m.strip()]
            if ms:
                out[ch] = ms
    return out or None


def battery(profile) -> dict:
    """{nominal_v, chemistry, assumed}: the bank as the profile says, else 12 V lead-acid with
    `assumed` True. A half-set block takes the default for the missing half (still `assumed`)."""
    b = block(profile).get("battery")
    b = b if isinstance(b, dict) else {}
    nv, chem = b.get("nominal_v"), b.get("chemistry")
    ok_v = _num(nv) and 6 <= nv <= 60
    ok_c = chem in CHEMISTRIES
    return {"nominal_v": float(nv) if ok_v else DEFAULT_BATTERY["nominal_v"],
            "chemistry": chem if ok_c else DEFAULT_BATTERY["chemistry"],
            "assumed": not (ok_v and ok_c)}


def battery_lines(profile) -> dict:
    """The bank watch's lines for this boat's bank: {floor_v, danger_margin_v, drain_level_v,
    drain_warn_v_per_h, charge_v_per_h} scaled from the 12 V set by the system voltage, plus the bank
    itself (nominal_v, chemistry, assumed). 12 V lead-acid gives exactly the historic lines."""
    bat = battery(profile)
    # the system class: a 12 V system is 12 V lead-acid or 12.8 V LiFePO4 (4 cells), 24 V is two
    # of those, and so on. A bank under 12 V (a 6 V pair's half) scales down.
    k = max(1, round(bat["nominal_v"] / 12.0)) if bat["nominal_v"] >= 9 else bat["nominal_v"] / 12.0
    lines = {name: round(v * k, 4) for name, v in _LINES_12V[_CURVE[bat["chemistry"]]].items()}
    return dict(lines, **bat)


def has_orin(profile) -> bool:
    """False only when the profile says there is no Orin on this boat."""
    return block(profile).get("orin") is not False


def plotter(profile) -> dict:
    """{present, model, max_route_waypoints}. `present` False only when the profile says there is
    no plotter. `max_route_waypoints` is the plotter's per-route limit, capped at what one N2K
    route can carry; None when the profile doesn't say."""
    p = block(profile).get("plotter")
    if p is False:
        return {"present": False, "model": None, "max_route_waypoints": None}
    p = p if isinstance(p, dict) else {}
    model = p.get("model") if isinstance(p.get("model"), str) and p.get("model").strip() else None
    mx = p.get("max_route_waypoints")
    mx = int(min(mx, PROTOCOL_MAX_WAYPOINTS)) if _num(mx) and mx >= 2 else None
    return {"present": True, "model": model, "max_route_waypoints": mx}


def errors(profile) -> list:
    """Shape errors in the profile's `hardware` block ([] when absent or fine). A carried profile
    with any is refused like any other bad field (shared/boat_profile.shape_errors)."""
    if not isinstance(profile, dict) or profile.get("hardware") is None:
        return []
    hw = profile["hardware"]
    if not isinstance(hw, dict):
        return ["bad hardware"]
    errs = []
    sp = hw.get("source_priority")
    if sp is not None and not (isinstance(sp, dict) and all(
            isinstance(ch, str) and isinstance(ms, list) and all(isinstance(m, str) for m in ms)
            for ch, ms in sp.items())):
        errs.append("bad hardware.source_priority")
    b = hw.get("battery")
    if b is not None:
        if not isinstance(b, dict):
            errs.append("bad hardware.battery")
        else:
            nv = b.get("nominal_v")
            if nv is not None and not (_num(nv) and 6 <= nv <= 60):
                errs.append("hardware.battery.nominal_v must be between 6 and 60")
            if b.get("chemistry") is not None and b["chemistry"] not in CHEMISTRIES:
                errs.append(f"hardware.battery.chemistry must be one of {', '.join(CHEMISTRIES)}")
    if hw.get("orin") is not None and not isinstance(hw["orin"], bool):
        errs.append("bad hardware.orin")
    p = hw.get("plotter")
    if p is not None and p is not False:
        if not isinstance(p, dict):
            errs.append("bad hardware.plotter")
        else:
            if p.get("model") is not None and not isinstance(p["model"], str):
                errs.append("bad hardware.plotter.model")
            mx = p.get("max_route_waypoints")
            if mx is not None and not (_num(mx) and 2 <= mx <= PROTOCOL_MAX_WAYPOINTS):
                errs.append(f"hardware.plotter.max_route_waypoints must be between 2 and "
                            f"{PROTOCOL_MAX_WAYPOINTS}")
    return errs


# ---- the hardware seed baked into the Pi engine image -------------------------------------------
# The Pi image carries no boat data (#304), but a profile stored aboard before #313 has no hardware
# block, and without one the boat's calibrated sensor priority would silently go. So the image
# carries each bundled boat's hardware block ONLY, one file per boat id ({boat_id, hardware}), used
# for a stored profile of that same boat id that has no block. Never another boat's.
def seed_files(src_dir, out_dir):
    """Write `<out_dir>/<boat_id>.json` = {boat_id, hardware} for each profile in `src_dir` that
    has a hardware block. Returns the boat ids written."""
    import glob
    import json
    import os
    os.makedirs(out_dir, exist_ok=True)
    out = []
    for path in sorted(glob.glob(os.path.join(src_dir, "*.json"))):
        try:
            with open(path) as f:
                p = json.load(f)
        except (OSError, ValueError):
            continue
        bid = p.get("boat_id") if isinstance(p, dict) else None
        if not isinstance(bid, str) or not bid or "/" in bid or bid.startswith("."):
            continue
        if not isinstance(p.get("hardware"), dict) or errors(p):
            continue
        with open(os.path.join(out_dir, f"{bid}.json"), "w") as f:
            json.dump({"boat_id": bid, "hardware": p["hardware"]}, f, indent=1, sort_keys=True)
        out.append(bid)
    return out


if __name__ == "__main__":         # python3 -m shared.boat_hardware <boats dir> <out dir>
    import sys
    if len(sys.argv) != 3:
        raise SystemExit("usage: python3 -m shared.boat_hardware <boats dir> <out dir>")
    print("hardware seed:", ", ".join(seed_files(sys.argv[1], sys.argv[2])) or "none")
