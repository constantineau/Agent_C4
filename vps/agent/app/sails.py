"""Sail-range advice from the SR33 ORC Speed Guide.

Parses the per-TWS tables in knowledge/sr33_speed_guide.md (each row carries the optimal
Sail at that TWA) into contiguous sail ZONES per wind speed, then answers: given live TWS
and TWA, which sail is optimal, where the boat sits within that sail's TWA band, and how
far to the next crossover/peel. Feeds the iPad sail-range dial and the get_sail_advice tool.

The inventory is jib (J1) upwind → A2 (tight reach) → A3 (broad reach) → S2 (run); the
crossover TWAs shift with TWS, so zones are recomputed from the nearest TWS bucket.
"""
import os
import re

_GUIDE = os.path.join(os.path.dirname(__file__), "..", "knowledge", "sr33_speed_guide.md")

# Friendly order + colors (hex) used by the dial, keyed by sail family prefix.
SAIL_ORDER = ["J1", "A2", "A3", "S2"]
SAIL_LABEL = {"J1": "Jib (J1)", "A2": "Asym A2", "A3": "Asym A3", "S2": "Kite (S2)"}


def _num(s):
    try:
        return float(s.strip().rstrip("°"))
    except (ValueError, AttributeError):
        return None


def _family(sail):
    """'A3-A' -> 'A3'; tolerant of user input like 'a3', 'A3 asym'."""
    if not sail:
        return None
    s = sail.strip().upper()
    for fam in SAIL_ORDER:
        if s.startswith(fam):
            return fam
    return None


def _parse_guide():
    """{tws: [ {twa, sail, btv, vmg, awa, heel}, ... ]} sorted by twa."""
    sections, tws = {}, None
    try:
        lines = open(_GUIDE).read().splitlines()
    except OSError:
        return {}
    for line in lines:
        m = re.match(r"##\s*TWS\s*([\d.]+)\s*kn", line)
        if m:
            tws = float(m.group(1))
            sections[tws] = []
            continue
        if tws is None or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or not re.match(r"[\d.]+°?$", cells[0]):
            continue  # header / separator / malformed
        sections[tws].append({
            "twa": _num(cells[0]), "btv": _num(cells[1]), "vmg": _num(cells[2]),
            "awa": _num(cells[4]), "heel": _num(cells[5]), "sail": _family(cells[8]),
        })
    for rows in sections.values():
        rows.sort(key=lambda r: r["twa"])
    return sections


_SECTIONS = _parse_guide()


def _nearest_tws(tws):
    if not _SECTIONS:
        return None
    return min(_SECTIONS.keys(), key=lambda k: abs(k - tws))


def _zones(rows):
    """Contiguous TWA bands per sail, with crossover boundaries at the midpoint between the
    last row of one sail and the first row of the next. Extends to 0 / 180 at the ends."""
    pts = [(r["twa"], r["sail"]) for r in rows if r["sail"]]
    if not pts:
        return []
    # group consecutive same-sail rows
    groups = []
    for twa, sail in pts:
        if groups and groups[-1]["sail"] == sail:
            groups[-1]["hi_twa"] = twa
        else:
            groups.append({"sail": sail, "lo_twa": twa, "hi_twa": twa})
    zones = []
    for i, gp in enumerate(groups):
        lo = 0.0 if i == 0 else round((groups[i - 1]["hi_twa"] + gp["lo_twa"]) / 2, 1)
        hi = 180.0 if i == len(groups) - 1 else round((gp["hi_twa"] + groups[i + 1]["lo_twa"]) / 2, 1)
        zones.append({"sail": gp["sail"], "label": SAIL_LABEL.get(gp["sail"], gp["sail"]),
                      "twa_min": lo, "twa_max": hi})
    return zones


def _nearest_row(rows, twa):
    return min(rows, key=lambda r: abs(r["twa"] - twa)) if rows else None


def get_sail_advice(tws: float = None, twa: float = None, hoisted: str = None):
    """Sail-range advice for the dial + agent. tws/twa are live values; hoisted is the
    crew-reported sail (J1/A2/A3/S2) so we can flag flying the wrong one."""
    if not _SECTIONS:
        return {"available": False, "note": "speed guide not loaded"}
    if tws is None or twa is None:
        return {"available": False, "note": "need live TWS and TWA to place the sail",
                "have": {"tws": tws, "twa": twa}}

    key = _nearest_tws(tws)
    rows = _SECTIONS[key]
    zones = _zones(rows)
    a_twa = abs(twa)
    tack = "port" if twa < 0 else "stbd"

    cur = next((z for z in zones if z["twa_min"] <= a_twa <= z["twa_max"]), None)
    optimal = cur["sail"] if cur else (_nearest_row(rows, a_twa) or {}).get("sail")
    # next crossover heading lower (heading up) and higher (bearing away) TWA
    up = max((z for z in zones if z["twa_max"] < a_twa), key=lambda z: z["twa_max"], default=None)
    down = min((z for z in zones if z["twa_min"] > a_twa), key=lambda z: z["twa_min"], default=None)
    next_xover = None
    if down:
        next_xover = {"at_twa": down["twa_min"], "to_sail": down["sail"],
                      "deg_away": round(down["twa_min"] - a_twa, 1), "direction": "bear away"}
    row = _nearest_row(rows, a_twa)
    hoisted_fam = _family(hoisted)
    wrong = hoisted_fam is not None and optimal is not None and hoisted_fam != optimal

    if wrong:
        rec = (f"Optimal sail is {SAIL_LABEL.get(optimal, optimal)} but {SAIL_LABEL.get(hoisted_fam, hoisted_fam)} "
               f"is up — peel/change to {SAIL_LABEL.get(optimal, optimal)}.")
    elif next_xover and next_xover["deg_away"] <= 8:
        rec = (f"{SAIL_LABEL.get(optimal, optimal)} is right; {next_xover['to_sail']} peel coming up at "
               f"{next_xover['at_twa']}° TWA ({next_xover['deg_away']}° away).")
    else:
        rec = f"{SAIL_LABEL.get(optimal, optimal)} is the right sail for {round(a_twa)}° TWA / {round(tws)} kn."

    return {
        "available": True,
        "tws_used": key, "twa": round(twa, 1), "twa_abs": round(a_twa, 1), "tack": tack,
        "zones": zones,
        "optimal_sail": optimal,
        "hoisted_sail": hoisted_fam,
        "wrong_sail": wrong,
        "in_range": (not wrong) if cur else False,
        "next_crossover": next_xover,
        "targets": None if not row else {"btv": row["btv"], "vmg": row["vmg"],
                                          "awa": row["awa"], "heel": row["heel"]},
        "recommendation": rec,
        "note": f"Zones from the nearest TWS bucket ({key} kn); crossovers shift with breeze.",
    }
