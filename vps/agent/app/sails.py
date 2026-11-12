"""Sail-range advice from the boat's speed guide.

Reads the per-TWS speed-guide tables (each row carries the optimal Sail at that TWA) as contiguous
sail ZONES per wind speed, then answers: given live TWS and TWA, which sail is optimal, where the
boat sits within that sail's TWA band, and how far to the next crossover/peel. Feeds the iPad
sail-range dial and the get_sail_advice tool.

The guide is the BOAT's (#304). Aboard (`DATA_SOURCE=onboard`) it is the deployed boat's, carried
by the Lab's Deploy push with its polar (`orinassets.boat_polar()`); nothing in the image is read.
On the shore agent it is the files named for its `BOAT_ID` (`knowledge/<boat>_speed_guide.md`,
`<boat>_crossovers.json`). Neither → "No boat polar loaded.", never another boat's guide.

The crossover TWAs shift with TWS, so zones are recomputed from the nearest TWS bucket.
"""
import contextvars
import json
import os

from shared import boat_polar as BP
from shared import sail_vocab as SV
from shared import units

_HERE = os.path.dirname(__file__)
_KNOWLEDGE = os.path.join(_HERE, "..", "knowledge")
_SEED = os.path.join(_HERE, "..", "..", "db", "seed")

# Sail names are the boat's own (#305): the speed guide's rows name its rated sails, the boat
# model's `sail_inventory` the crew's (jib change-downs, a Code 0, a staysail; shared/sail_vocab.py).
# Logic keys on each sail's family, text uses its name.
_FAMILY_LABEL = {"jib": "Jib ({})", "genoa": "Genoa ({})", "asym": "Asym {}", "sym": "Kite ({})"}


def _family(sail):
    """A guide row's sail name: 'A3-A' -> 'A3'; tolerant of input like 'a3', 'A3 asym'. None for
    a blank or '—' cell."""
    t = SV.token(sail)
    return t if t and any(c.isalnum() for c in t) else None


def _guide_sails(sections=None):
    """The rated sails the speed guide names, in first-seen order."""
    sections = _guide()[0] if sections is None else sections
    out = []
    for k in sorted(sections):
        for r in sections[k]:
            if r.get("sail") and r["sail"] not in out:
                out.append(r["sail"])
    return out


def label(sail, boat_model=None, rated=None):
    """The sail's display name: 'Code 0' for the boat's Code 0, the family word around a RATED
    sail's name ('Jib (J1)', 'Asym A3'), else the name as the boat calls it."""
    if not sail:
        return sail
    fam = SV.family(sail, boat_model)
    if fam == "code0":
        return "Code 0"
    rated = _guide_sails() if rated is None else rated
    if sail in rated and fam in _FAMILY_LABEL:
        return _FAMILY_LABEL[fam].format(sail)
    return sail


def _cert_equiv(crew, rated, boat_model=None):
    """The rated sail a crew sail flies as: itself if rated, a jib change-down as the rated
    headsail, a kite as the rated kite of its family. None for a Code 0 / staysail (no rating)."""
    if not crew:
        return None
    if crew in rated:
        return crew
    fam = SV.family(crew, boat_model)
    fams = SV.HEADSAILS if fam in SV.HEADSAILS else ((fam,) if fam in SV.KITES else ())
    return next((r for r in rated if SV.family(r, boat_model) in fams), None)


def _sections_of(guide):
    """{"<tws>": [rows]} (a polar block's speed guide) → {tws: [{twa, sail(family), …}]}."""
    out = {}
    for k, rows in (guide or {}).items():
        try:
            tws = float(k)
        except (TypeError, ValueError):
            continue
        # `raw` keeps the cell as written: a boat's own name (BIG-RED) is matched against its
        # inventory per answer (`_resolved`); `sail` is the shorthand token until then
        rs = [dict(r, raw=r.get("sail"), sail=_family(r.get("sail"))) for r in rows or ()
              if isinstance(r, dict) and isinstance(r.get("twa"), (int, float))]
        if rs:
            out[tws] = sorted(rs, key=lambda r: r["twa"])
    return out


def _resolved(sections, src):
    """`sections` with each row's sail named as the boat names it: the longest inventory name the
    cell starts with ('BIG-RED' stays BIG-RED, 'A3-A' → A3), else the shorthand token. Unchanged
    when the boat lists no inventory."""
    if not SV.names(src):
        return sections
    out = {}
    for k, rows in sections.items():
        out[k] = [dict(r, sail=SV.match(r.get("raw") or r.get("sail") or "", src) or r.get("sail"))
                  for r in rows]
    return out


def _overlaps_of(xo):
    """{tws_bucket:int -> [ {sails:[s1,s2], twa_min, twa_max}, … ]} from a crossover table, or {}."""
    try:
        return {int(float(k)): v for k, v in ((xo or {}).get("overlaps") or {}).items()}
    except (TypeError, ValueError, AttributeError):
        return {}


_MEMO = {"memo": None}          # (key, (sections, overlaps)), replaced as one tuple
# one answer reads ONE guide: a Deploy push landing mid-answer can't mix two boats (Codex on #319)
_PINNED = contextvars.ContextVar("sails_guide", default=None)


def _shore_files():
    bid = (os.environ.get("BOAT_ID") or "").strip()
    if not bid or not BP.name_ok(f"{bid}_speed_guide.md"):
        return None, None
    guide = os.path.join(_KNOWLEDGE, f"{bid}_speed_guide.md")
    xo = next((p for p in (os.path.join(_KNOWLEDGE, f"{bid}_crossovers.json"),
                           os.path.join(_SEED, f"{bid}_crossovers.json")) if os.path.exists(p)),
              None)
    return (guide if os.path.exists(guide) else None), xo


def _guide():
    """(sections, overlaps) of THIS boat's speed guide (see the module doc); ({}, {}) when none.
    Cached as ONE (key, guide) tuple, built locally and published at once, so readers racing a
    Deploy push never leave one boat's guide under another's key (Codex on #319)."""
    pinned = _PINNED.get()
    if pinned is not None:
        return pinned
    if os.environ.get("DATA_SOURCE", "").strip().lower() == "onboard":
        from . import orinassets
        block = orinassets.boat_polar()
        key = ("deploy", (block or {}).get("version"))
        memo = _MEMO["memo"]
        if memo is not None and memo[0] == key:
            return memo[1]
        guide = (_sections_of((block or {}).get("speed_guide")),
                 _overlaps_of((block or {}).get("crossovers")))
        _MEMO["memo"] = (key, guide)
        return guide
    g, x = _shore_files()
    key = ("shore", g, x)
    memo = _MEMO["memo"]
    if memo is not None and memo[0] == key:
        return memo[1]
    sections, overlaps = {}, {}
    if g:
        try:
            with open(g, encoding="utf-8") as f:
                sections = _sections_of(BP.parse_speed_guide(f.read()))
        except OSError:
            sections = {}
    if x:
        try:
            with open(x) as f:
                overlaps = _overlaps_of(json.load(f))
        except (OSError, ValueError):
            overlaps = {}
    guide = (sections, overlaps)
    _MEMO["memo"] = (key, guide)
    return guide


def _overlaps_for(tws_bucket):
    """The toss-up bands for a TWS bucket (nearest key), TWA-sorted."""
    overlaps = _guide()[1]
    if not overlaps or tws_bucket is None:
        return []
    b = min(overlaps.keys(), key=lambda k: abs(k - tws_bucket))
    return sorted(overlaps.get(b, []), key=lambda o: o.get("twa_min", 0))


def _nearest_tws(tws):
    sections = _guide()[0]
    if not sections:
        return None
    return min(sections.keys(), key=lambda k: abs(k - tws))


def _zones(rows, fam_src=None):
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
        zones.append({"sail": gp["sail"], "label": label(gp["sail"], fam_src),
                      "twa_min": lo, "twa_max": hi})
    return zones


def _nearest_row(rows, twa):
    return min(rows, key=lambda r: abs(r["twa"] - twa)) if rows else None


def _crew_norm(sail, boat_model=None, rated=()):
    """The boat's own name for the crew-set sail ('a2' -> 'A2'), or None if it has no such sail."""
    return SV.match(sail, boat_model, extra=rated)


def _tws_band_for(fam, a_twa):
    """The TWS buckets where `fam` wins the zone at this TWA (cert model) — the over/under test."""
    ks = []
    sections = _guide()[0]
    for k in sorted(sections):
        z = next((z for z in _zones(sections[k]) if z["twa_min"] <= a_twa <= z["twa_max"]), None)
        if z and z["sail"] == fam:
            ks.append(k)
    return ks


def _fit_and_change(crew, a_twa, tws, optimal, wrong, tossup_ok, boat_model, fam_src=None):
    """How the CREW-SET sail fits the conditions right now, and what to change to if it doesn't:
    (fit, change_to). fit ∈ in_range | overpowered | underpowered | wrong_angle | no_model | None.
    Jib TWS bands + the Code-0 envelope come from the frozen boat model when one is aboard; the
    staysail flies WITH another sail and has no solo rating (logged only)."""
    bm = boat_model or {}
    if crew is None:
        return None, None
    fam = SV.family(crew, fam_src if fam_src is not None else bm)   # thresholds stay bm's
    if fam == "staysail":
        return "no_model", None
    if fam == "code0":
        # the frozen bundle keeps the C0 band at boat_model["code0"] (synthesis.boat_model) — #227.
        # Same rule as the Lab's sailplan._code0_band: off only when enabled is False, and a band
        # missing any limit is no band at all (never guess 0–180° / 99 kn).
        c0 = bm.get("code0") or {}
        if (c0.get("enabled") is False
                or any(c0.get(k) is None for k in ("tws_max", "twa_min", "twa_max"))):
            return "no_model", None
        if not (float(c0["twa_min"]) <= a_twa <= float(c0["twa_max"])):
            return "wrong_angle", optimal
        if tws > float(c0["tws_max"]):
            return "overpowered", optimal
        return "in_range", None
    if fam in SV.HEADSAILS:
        if wrong and not tossup_ok:            # the cert wants a different FAMILY at this angle
            return "wrong_angle", optimal
        jx = bm.get("jib_crossovers") or []
        band = next((b for b in jx if (b.get("sail") or "").upper() == crew), None)
        right = next((b.get("sail") for b in jx
                      if float(b.get("tws_min") or 0) <= tws <= float(b.get("tws_max") or 99)), None)
        if band:
            if band.get("tws_max") is not None and tws > float(band["tws_max"]):
                return "overpowered", right or optimal
            if band.get("tws_min") is not None and tws < float(band["tws_min"]):
                return "underpowered", right or optimal
        return "in_range", None
    # kites: the cert knows their whole envelope
    if wrong and not tossup_ok:
        band = _tws_band_for(crew, a_twa)
        if band:
            if tws > max(band):
                return "overpowered", optimal
            if tws < min(band):
                return "underpowered", optimal
        return "wrong_angle", optimal
    return "in_range", None


def _names_src(boat_model):
    """The boat's sail words, resolved ONCE (shared/sail_vocab.merge): names from the frozen boat
    model when it lists them, else the boat's profile (app/boatinfo.py); `sail_families` from both
    (the frozen model wins a clash). Words only: the fit THRESHOLDS read `boat_model` alone."""
    prof = None
    try:
        from . import boatinfo
        prof = boatinfo.profile()
    except Exception:
        prof = None
    return SV.merge(boat_model, prof)


def _sail_list(src, rated=()):
    """The boat's sails for the dial's buttons and colours: [{name, family}]."""
    inv = SV.inventory(src)
    have = {e["name"] for e in inv}
    inv += [{"name": r, "family": SV.family(r, src) or "other"} for r in rated if r not in have]
    return inv


def get_sail_advice(tws: float = None, twa: float = None, hoisted: str = None, boat_model: dict = None):
    """Sail-range advice for the dial + agent (see `_advice`), on one snapshot of the guide, its
    sails named in the boat's own words (resolved once for this answer)."""
    src = _names_src(boat_model)
    sections, overlaps = _guide()
    tok = _PINNED.set((_resolved(sections, src), overlaps))
    try:
        return _advice(tws, twa, hoisted, boat_model, src)
    finally:
        _PINNED.reset(tok)


def _advice(tws: float = None, twa: float = None, hoisted: str = None, boat_model: dict = None,
            src: dict = None):
    """Sail-range advice for the dial + agent. tws/twa are live values; hoisted is the
    crew-reported sail (the boat's own name) so we can flag flying the wrong one."""
    sections = _guide()[0]
    if not sections:
        note = BP.NO_POLAR_ABOARD
        if os.environ.get("DATA_SOURCE", "").strip().lower() == "onboard":
            from . import orinassets
            if orinassets.boat_polar() is None:
                note = BP.NO_POLAR      # the boat being sailed has none (#320: e.g. an Other boat)
        # no sail model, but the crew-set sail is still named in the boat's own words (#305)
        src = _names_src(boat_model) if src is None else src
        return {"available": False, "note": note, "crew_sail": _crew_norm(hoisted, src),
                "sails": _sail_list(src)}
    if tws is None or twa is None:
        # no wind, no call — but the boat's sails still list, so the crew can set what's flying
        # and mark gear out with no wind sensor (Codex on #334)
        src = _names_src(boat_model) if src is None else src
        rated = _guide_sails(sections)
        return {"available": False, "note": "Need live wind speed and angle.",
                "have": {"tws": tws, "twa": twa},
                "crew_sail": _crew_norm(hoisted, src, rated), "sails": _sail_list(src, rated)}

    key = _nearest_tws(tws)
    rows = sections[key]
    src = _names_src(boat_model) if src is None else src   # the boat's sail words
    zones = _zones(rows, src)
    # The record wraps TWA to 0–360 (a port beat at 36° arrives as 324°): take it signed first,
    # or abs() reads a beat as a run and flags the right jib wrong (#47).
    twa = units.signed_twa(twa)
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
    rated = _guide_sails(sections)
    crew_sail = _crew_norm(hoisted, src, rated)
    hoisted_fam = _cert_equiv(crew_sail, rated, src)

    # toss-up: sails within ~1.5% of target AT this TWA (from the crossover overlap bands). The zones are
    # winner-take-all, so a near-tied sail reads as "wrong" — but if the hoisted sail is a toss-up with the
    # optimal one, it's fine: carry either, no need to peel.
    overlaps = _overlaps_for(key)
    cooptimal = set()
    for o in overlaps:
        if o["twa_min"] <= a_twa <= o["twa_max"]:
            cooptimal.update(o.get("sails", []))
    tossup_with = sorted(s for s in cooptimal if s != optimal) if optimal in cooptimal else []
    hoisted_ok_tossup = (hoisted_fam is not None and hoisted_fam != optimal
                         and optimal in cooptimal and hoisted_fam in cooptimal)
    wrong = (hoisted_fam is not None and optimal is not None
             and hoisted_fam != optimal and not hoisted_ok_tossup)

    # ONE decision drives both the console tile and this text (#113): the fit of the crew-set
    # sail. The angle check alone passes an overpowered J1, so the text used to say "J1 is right"
    # under a tile saying "change to J2".
    fit, change_to = _fit_and_change(crew_sail, a_twa, tws, optimal, wrong,
                                     hoisted_ok_tossup, boat_model, fam_src=src)
    change_now = fit in ("overpowered", "underpowered", "wrong_angle")
    if change_now and not change_to:
        change_to = optimal              # the tile's fallback, made explicit so both agree
    # the crew's own jib when it is the right family and fits (J2 up: say J2, not "J1 is right")
    best = (crew_sail if (fit == "in_range" and _cert_equiv(crew_sail, rated, src) == optimal)
            else optimal)

    _lbl = lambda s: label(s, src, rated)
    why = {"overpowered": "Too much wind for {}.", "underpowered": "Not enough wind for {}.",
           "wrong_angle": "{} is wrong for this angle."}
    if change_now:
        rec = f"Change to {_lbl(change_to)}. " + why[fit].format(_lbl(crew_sail))
    elif fit == "in_range" and SV.family(crew_sail, src) == "code0":
        # the cert has no Code 0, so `best` would name the cert's sail here — say the C0 is right
        rec = f"{_lbl(crew_sail)} is right for {round(a_twa)}° TWA, {round(tws)} kts."
    elif wrong:
        rec = f"Change to {_lbl(optimal)}. {_lbl(hoisted_fam)} is up."
    elif hoisted_ok_tossup:
        rec = f"{_lbl(hoisted_fam)} is fine. {_lbl(optimal)} is equal here, no need to change."
    elif next_xover and next_xover["deg_away"] <= 8:
        rec = (f"{_lbl(best)} is right. {next_xover['to_sail']} at "
               f"{next_xover['at_twa']}° TWA ({next_xover['deg_away']}° away).")
    elif tossup_with:
        rec = f"{_lbl(best)} or {', '.join(_lbl(s) for s in tossup_with)}. Either works here."
    else:
        rec = f"{_lbl(best)} for {round(a_twa)}° TWA, {round(tws)} kts."

    return {
        "available": True,
        "tws_used": key, "twa": round(twa, 1), "twa_abs": round(a_twa, 1), "tack": tack,
        "zones": zones,
        "overlaps": overlaps,
        "optimal_sail": optimal,
        "tossup_with": tossup_with,
        "crew_sail": crew_sail,          # what the crew SET (sails bar), in the boat's own names
        "sails": _sail_list(src, rated),  # the boat's sails, with families (the dial's buttons)
        "fit": fit,                       # in_range | overpowered | underpowered | wrong_angle | no_model
        "change_to": change_to,
        "hoisted_sail": hoisted_fam,
        "wrong_sail": wrong,
        "in_range": (not wrong) if cur else False,
        "next_crossover": next_xover,
        "targets": None if not row else {"btv": row["btv"], "vmg": row["vmg"],
                                          "awa": row["awa"], "heel": row["heel"]},
        "recommendation": rec,
        "note": f"Sail ranges for {key} kts.",
    }
