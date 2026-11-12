"""Sail crossover model — routing fidelity 2b (the per-leg SAIL PLAN source).

The Lab optimizer routes on the Best-Performance polar envelope, which IS the max-over-sails speed —
so the route's SPEED is already sail-optimal, but the route doesn't say WHICH sail achieves it. This
module supplies that: the per-(TWS,TWA) optimal sail + the per-TWS crossover bands, precomputed from
the ORC certificate (by `vps/agent/knowledge/build_speed_guide.py`). The optimizer attaches `sail`
to each leg; the playbook bundle freezes the whole crossover table as the reviewed, onboard-loadable
boat sail model.

The table is the BOAT's (#303): callers pass `xo`, the `crossovers` of the boat's polar block
(`boats.polar_for(boat)`, see `of`). No table (a cert-only boat) → no sail calls, never another
boat's.
"""

from shared import sail_vocab

_EMPTY = {"crossovers": {}, "tws_buckets": [], "inventory": [], "sail_names": {}}


def of(block):
    """The crossover table of a boat polar block (None when it has none)."""
    xo = (block or {}).get("crossovers")
    return xo if isinstance(xo, dict) else None


def _load(xo):
    return xo if isinstance(xo, dict) else _EMPTY


def available(xo) -> bool:
    return bool(_load(xo).get("crossovers"))


def model(xo) -> dict:
    """The full sail model (for the bundle's boat_model block + the review UI)."""
    d = _load(xo)
    return {"boat_id": d.get("boat_id"), "source": d.get("source"),
            "inventory": d.get("inventory", []), "sail_names": d.get("sail_names", {}),
            "tws_buckets": d.get("tws_buckets", []), "crossovers": d.get("crossovers", {}),
            "overlaps": d.get("overlaps", {})}


def _nearest_tws(tws, xo):
    buckets = _load(xo).get("tws_buckets") or []
    if not buckets or tws is None:
        return None
    return min(buckets, key=lambda b: abs(b - tws))


def crossovers(tws, xo):
    """The sail zones for the nearest TWS bucket: [{sail, short, label, twa_min, twa_max}]."""
    b = _nearest_tws(tws, xo)
    return _load(xo).get("crossovers", {}).get(str(b), []) if b is not None else []


def crossovers_specialized(jib_crossovers, sail_config=None, xo=None):
    """The full per-TWS crossover table with the upwind jib band relabelled to the actual jib for
    THAT row's TWS (J1/J2/J3 by the boat's change-downs) — so the chart shows what you'd really fly,
    not just the cert's single J1. Each row IS one TWS, so the substitution is exact (not a clamp).
    With a `sail_config` carrying a Code 0 band, the C0 CARVES its TWA range out of the jib zone on
    the light rows it applies to (the zone splits jib / C0 / jib as needed) — same crew-band-overlay
    discipline as the jib change-downs (the cert doesn't rate a Code 0)."""
    raw = _load(xo).get("crossovers", {})
    c0 = _code0_band(sail_config)
    if not jib_crossovers and not c0:
        return raw
    out = {}
    for tws_key, zones in raw.items():
        try:
            tws = float(tws_key)
        except ValueError:
            out[tws_key] = zones
            continue
        new = []
        for z in zones:
            base = z.get("short") or z.get("sail")
            if _is_headsail(base, sail_config):
                jib = jib_for_tws(tws, jib_crossovers, base) if jib_crossovers else base
                z = {**z, "sail": jib, "short": jib,
                     "label": _jib_label(jib, base, z.get("label"), sail_config)}
            new.extend(_carve_code0(z, tws, c0, code0_name(sail_config)))
        out[tws_key] = _merge_c0(new, code0_name(sail_config))
    return out


def code0_name(sail_config):
    """The boat's own name for its Code 0 (`code0_sail` in the sail config, boats.sail_config),
    else the shorthand "C0" (#305)."""
    n = (sail_config or {}).get("code0_sail")
    return n if isinstance(n, str) and n.strip() else "C0"


def _code0_band(sail_config):
    """The Code 0 band {tws_max, twa_min, twa_max} if configured + enabled, else None."""
    c0 = (sail_config or {}).get("code0") or {}
    if not c0 or c0.get("enabled") is False:
        return None
    if c0.get("tws_max") is None or c0.get("twa_min") is None or c0.get("twa_max") is None:
        return None
    return c0


def _carve_code0(zone, tws, c0, name="C0"):
    """Split one cert zone against the Code 0 band for this row's TWS: returns 1–3 zones (below /
    C0 / above). The band is the CREW'S call, so it carves whatever cert sail it overlaps (jib or
    light kite) — labels only, the rated envelope speed underneath is untouched. No overlap
    (heavier air or disjoint TWA) → the zone unchanged."""
    if not c0 or tws > float(c0["tws_max"]):
        return [zone]
    lo = max(float(zone["twa_min"]), float(c0["twa_min"]))
    hi = min(float(zone["twa_max"]), float(c0["twa_max"]))
    if lo >= hi:
        return [zone]
    out = []
    if float(zone["twa_min"]) < lo:
        out.append({**zone, "twa_max": lo})
    out.append({**zone, "sail": name, "short": name, "twa_min": lo, "twa_max": hi,
                "label": "Code 0 (light-air reacher)"})
    if float(zone["twa_max"]) > hi:
        out.append({**zone, "twa_min": hi})
    return out


def _merge_c0(zones, name="C0"):
    """Adjacent Code 0 fragments (the band carved across a jib/kite boundary) merge into one zone."""
    out = []
    for z in zones:
        if (out and z.get("short") == name and out[-1].get("short") == name
                and abs(float(out[-1]["twa_max"]) - float(z["twa_min"])) < 0.51):
            out[-1] = {**out[-1], "twa_max": z["twa_max"]}
        else:
            out.append(z)
    return out


def overlaps_specialized(jib_crossovers, xo=None, sail_config=None):
    """The per-TWS toss-up bands with the upwind jib in each band's sail list relabelled to the actual
    jib for THAT row's TWS (J1/J2/J3) — mirrors crossovers_specialized so the chart's overlaps match."""
    raw = _load(xo).get("overlaps", {})
    if not jib_crossovers:
        return raw
    out = {}
    for tws_key, bands in raw.items():
        try:
            tws = float(tws_key)
        except ValueError:
            out[tws_key] = bands
            continue
        out[tws_key] = [{**b, "sails": [jib_for_tws(tws, jib_crossovers, s)
                                        if _is_headsail(s, sail_config) else s
                                        for s in b.get("sails", [])]}
                        for b in bands]
    return out


def _is_headsail(sail, sail_config=None):
    """The cert's upwind headsail slot — the one its jib (or genoa) change-downs share (#305: by the
    sail's family — the boat's `sail_families`, else the shorthand — not a fixed name)."""
    return sail_vocab.family(sail, sail_config) in sail_vocab.HEADSAILS


def _jib_label(jib, rated, cert_label, sail_config=None):
    if jib == rated:
        return cert_label
    return f"{'Genoa' if sail_vocab.family(jib, sail_config) == 'genoa' else 'Jib'} {jib}"


def jib_for_tws(tws, jib_crossovers, rated=None):
    """Pick the specific upwind headsail (e.g. J1/J2/J3) for a TWS from the boat's change-down
    bands. The ORC cert rates only one (`rated`, the crossover table's), so this TWS split is the
    crew's, not the polar's. Falls back to the rated headsail if no band matches / none set."""
    if not jib_crossovers or tws is None:
        return rated
    for b in jib_crossovers:
        lo = b.get("tws_min")
        hi = b.get("tws_max")
        if (lo is None or tws >= lo) and (hi is None or tws < hi):
            return b.get("sail") or rated
    return jib_crossovers[-1].get("sail") or rated


def optimal_sail(tws, twa, jib_crossovers=None, sail_config=None, xo=None):
    """The crew-shorthand sail (e.g. 'A3') optimal at (tws, twa). TWA is folded to 0–180 (port/
    starboard symmetric). A leg's TWA is the DIRECT-course angle, so an upwind beat can read below
    the close-hauled angle (you tack up on the beat sail) — clamp to the first/last band rather than
    returning nothing. When the sail is the upwind jib and `jib_crossovers` are given, specialise it
    to the boat's change-downs by TWS; when a Code 0 band (`sail_config.code0`) covers (tws, twa), the jib slot
    becomes 'C0' — a light-air label overlay, same discipline as the change-downs (routing speed
    stays the rated envelope). None only when the model is unavailable."""
    if twa is None:
        return None
    twa = abs(((twa + 180) % 360) - 180)
    zones = crossovers(tws, xo)
    if not zones:
        return None
    if twa < zones[0]["twa_min"]:                     # below the beat angle → beating on the up sail
        base = zones[0].get("short") or zones[0].get("sail")
    else:
        base = zones[-1].get("short") or zones[-1].get("sail")   # past the last band → the run sail
        for z in zones:
            if z["twa_min"] <= twa <= z["twa_max"]:
                base = z.get("short") or z.get("sail")
                break
    c0 = _code0_band(sail_config)
    if (c0 and tws is not None and tws <= float(c0["tws_max"])
            and float(c0["twa_min"]) <= twa <= float(c0["twa_max"])):
        return code0_name(sail_config)   # the crew's light-air reacher band owns the call
    if jib_crossovers and _is_headsail(base, sail_config):
        return jib_for_tws(tws, jib_crossovers, base)
    return base


def reef_for(tws, point_of_sail, sail, main_reefs, sail_config=None):
    """The main reef the crew should carry at (tws, point_of_sail, headsail): {'reef','why'} or
    None. Two crew thresholds (not in the cert): `r1_tws_kn` — tuck in reef 1 to DEPOWER in breeze
    (any point of sail); `r1_a3_slot_tws_kn` — reef 1 when running an asymmetric kite to OPEN THE
    SLOT between the kite and the main (fires at a lower TWS; #305: any asym, named as the boat
    names it). Reefs decorate the sail CALL, never speed."""
    mr = main_reefs or {}
    if not mr or tws is None:
        return None
    slot = mr.get("r1_a3_slot_tws_kn")
    # an asym up already implies off-wind (nobody beats under a kite) — keying on the sail keeps this
    # robust to the two point-of-sail vocabularies (beat/reach/run vs upwind/reaching/downwind)
    if slot is not None and sail_vocab.family(sail, sail_config) == "asym" and tws >= float(slot):
        return {"reef": "R1", "why": f"open the slot between the {sail} and the main"}
    r1 = mr.get("r1_tws_kn")
    if r1 is not None and tws >= float(r1):
        return {"reef": "R1", "why": "depower in the breeze"}
    return None
