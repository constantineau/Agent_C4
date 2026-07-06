"""ORC public-cert `Allowances` → an optimizer-shaped polar.

Every ORC-certified boat's cert in the public dump (`fleetimport._orc_dump`) carries a full polar as
time allowances: seconds/nautical-mile at TWA 52–150° across TWS 4–16 kn, plus Beat/Run VMG
allowances with the optimum beat/gybe angles. `3600 / s_per_nm` → boatspeed (kts). Beat/Run rows are
VMG-basis (per mile MADE GOOD), so STW at the optimum angle = VMG / |cos(angle)|.

Output shape = the optimizer's canonical polar table `[(tws, twa, stw)]` (`polars.polars_stw()`),
so a retro fleet run can route any certified boat with `optimize_course(..., polar=cert_polar(rec))`.
"""
import math


def cert_polar(cert: dict) -> list:
    """[(tws_kn, twa_deg, stw_kn)] from one ORC cert record, [] if it has no Allowances."""
    a = (cert or {}).get("Allowances") or {}
    ws = a.get("WindSpeeds") or []
    if not ws:
        return []
    P = []

    # sailing-angle rows R52..R150 (WindAngles lists most; R150 is present even when omitted there)
    angles = list(a.get("WindAngles") or [])
    if 150 not in angles and a.get("R150"):
        angles.append(150)
    for ang in angles:
        row = a.get(f"R{int(ang)}") or []
        for w, s in zip(ws, row):
            try:
                s = float(s)
            except (TypeError, ValueError):
                continue
            if s > 0:
                P.append((float(w), float(ang), round(3600.0 / s, 3)))

    # Beat / Run: allowance is s/nm of distance MADE GOOD at the optimum angle
    for key, angkey in (("Beat", "BeatAngle"), ("Run", "GybeAngle")):
        row, angs = a.get(key) or [], a.get(angkey) or []
        for w, s, ang in zip(ws, row, angs):
            try:
                s, ang = float(s), float(ang)
            except (TypeError, ValueError):
                continue
            c = abs(math.cos(math.radians(ang)))
            if s > 0 and c > 0.2:
                P.append((float(w), ang, round((3600.0 / s) / c, 3)))

    P.sort()
    return P
