"""BoatProfile — the per-boat half of a regatta (race x boat = two independent dimensions).

A RaceDefinition describes the *course*; a BoatProfile describes the *boat* sailing it. The Lab keeps
them separate so the same race can be optimized for different boats and the SR33 work generalizes to
other boats. The immediate consumer is the optimizer's obstacle field: a boat's **draft** sets the
ENC depth no-go contour (any charted water shallower than draft + under-keel margin is blocked), so
a deeper boat gets a more conservative route over the very same chart data.

Canonical units are SI (metres) to match the rest of the engine and the raw-SI telemetry convention.
US sailors think in FEET, so the Lab UI displays/enters draft in feet and converts on the boundary
(`ft_to_m`/`m_to_ft`); only metres are stored.

Fields beyond draft (polars, ORC rating, beam/air-draft/displacement/hull/sail inventory) are carried
so the profile can grow into corrected-time scoring and fuller modelling — draft is the one wired into
routing today. SR33 = profile #1 (`vps/lab/boats/sr33.json`, draft 7 ft = 2.1336 m).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

SCHEMA_VERSION = "0.1"

HULL_TYPES = {"mono", "multi"}
DEFAULT_MARGIN_M = 0.5            # under-keel safety margin added to draft for the no-go depth
M_PER_FT = 0.3048


def ft_to_m(ft: float) -> float:
    return float(ft) * M_PER_FT


def m_to_ft(m: float) -> float:
    return float(m) / M_PER_FT


@dataclass
class Orc:
    """ORC handicap numbers (for corrected-time scoring later; optional)."""
    rating: Optional[float] = None       # ORC single-number (e.g. ToT coefficient)
    gph: Optional[float] = None          # General Purpose Handicap (s/nm)
    scoring: str = ""                    # e.g. "ToT" / "ToD" — usually per-race from the SI


@dataclass
class BoatProfile:
    boat_id: str
    name: str
    draft_m: float                                   # canonical metres (UI enters/shows feet)
    boat_class: str = ""                             # e.g. "SR33"
    sail_number: str = ""
    safety_margin_m: float = DEFAULT_MARGIN_M        # under-keel margin → no-go depth = draft + this
    polars_file: str = ""                            # per-boat polars (blank → the Lab default)
    orc: Optional[dict] = None
    # fuller modelling, optional / future:
    beam_m: Optional[float] = None
    air_draft_m: Optional[float] = None
    displacement_kg: Optional[float] = None
    hull_type: str = "mono"
    sail_inventory: list = field(default_factory=list)
    # Upwind jib change-downs by TWS (kn). The ORC cert rates only ONE headsail (the speed-optimal
    # J1), so J2/J3 — same upwind slot, smaller jibs for a building breeze — aren't in the polar;
    # these crew/sailmaker thresholds split the upwind jib by wind strength. [{sail, tws_min?, tws_max?}].
    jib_crossovers: list = field(default_factory=list)
    # Code 0 — the light-air reaching sail. NOT in the ORC cert (like J2/J3), so it is a crew-band
    # LABEL overlay: within {tws_max, twa_min, twa_max} the optimal-sail call becomes "C0" (it takes
    # the cert jib's slot in that band; routing SPEED stays the rated envelope — the band sets the
    # CALL, never an invented speed). {enabled, tws_max, twa_min, twa_max}; empty → no Code 0.
    code0: dict = field(default_factory=dict)
    # Mainsail reef points (kn TWS) — crew thresholds, not in the cert. r1_tws_kn = tuck in reef 1
    # to DEPOWER in breeze (any point of sail); r1_a3_slot_tws_kn = reef 1 when running with the A3
    # to OPEN THE SLOT between the kite and the main (fires at a lower TWS than the depower reef).
    # Reefs decorate the sail call ("A3 + R1"), they don't change routing speed. Empty → no reefs.
    main_reefs: dict = field(default_factory=dict)
    # Helm-skill factor (0–1): the fraction of the FLAT-WATER ORC polar this crew actually achieves —
    # the optimizer routes on ACHIEVABLE speed (helm × sea state), and the gap to 1.0 is a coaching
    # number. 1.0 = sails the book; the Lab-4 learning loop can refine it from real tracks. (2d-d)
    helm_factor: float = 1.0
    # Refined-polar overlay from the Lab-4 learning loop: human-APPROVED multiplicative tweaks to the
    # ORC cert per (TWS, TWA) cell — [{tws, twa, mult, basis?}]. The cert stays gospel (untouched);
    # this is an explicit, reviewable overlay applied at optimize time. Empty → routes on the raw cert.
    polar_adjustments: list = field(default_factory=list)
    # Sea-state degradation coefficients (Lab-4 calibration), per-boat overlay on the conservative env
    # priors. {hs_deadband, k_up, k_reach, k_down, floor} — the per-metre speed loss above the deadband,
    # by point of sail. Calibrated from the boat's realized-polar archive (learning.calibrate_waves) and
    # human-APPROVED; empty → the optimizer uses the ROUTE_WAVE_* env defaults. Keeps helm_factor a
    # FLAT-WATER number (the wave model carries the sea-state loss, so the two don't double-count).
    wave_coeffs: dict = field(default_factory=dict)
    note: str = ""
    provenance: dict = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def safety_depth_m(self) -> float:
        """No-go depth: draft + under-keel margin. Charted water shallower than this is blocked."""
        return float(self.draft_m) + float(self.safety_margin_m)


def from_dict(d: dict) -> BoatProfile:
    """Build a BoatProfile from a stored JSON dict, ignoring unknown keys."""
    known = {f for f in BoatProfile.__dataclass_fields__}            # noqa: SIM118
    return BoatProfile(**{k: v for k, v in (d or {}).items() if k in known})


def safety_depth_m(d: dict) -> float:
    """No-go depth for a profile dict: draft + under-keel margin (defaults if absent)."""
    draft = float(d.get("draft_m") or 0.0)
    margin = d.get("safety_margin_m")
    margin = float(margin) if margin is not None else DEFAULT_MARGIN_M
    return draft + margin


def summary(d: dict) -> dict:
    """List/selector view of a profile dict — adds feet conveniences (canonical store stays metres)."""
    draft_m = d.get("draft_m")
    return {
        "boat_id": d.get("boat_id"),
        "name": d.get("name"),
        "boat_class": d.get("boat_class", ""),
        "sail_number": d.get("sail_number", ""),
        "draft_m": draft_m,
        "draft_ft": round(m_to_ft(draft_m), 2) if draft_m is not None else None,
        "safety_margin_m": d.get("safety_margin_m", DEFAULT_MARGIN_M),
        "safety_depth_m": round(safety_depth_m(d), 2) if draft_m is not None else None,
        "hull_type": d.get("hull_type", "mono"),
        "helm_factor": d.get("helm_factor", 1.0),
        "polar_adjustments": d.get("polar_adjustments", []),
        "wave_coeffs": d.get("wave_coeffs") or {},
    }


def validate(d: dict):
    """(errors, warnings). Errors block use; warnings flag review (missing optional modelling)."""
    errors, warnings = [], []
    if not d.get("boat_id"):
        errors.append("missing boat_id")
    if not d.get("name"):
        warnings.append("missing name")
    draft = d.get("draft_m")
    if draft is None:
        errors.append("missing draft_m (required — sets the ENC depth no-go)")
    else:
        try:
            dm = float(draft)
            if dm <= 0:
                errors.append("draft_m must be positive")
            elif dm > 6.0:
                warnings.append(f"draft_m={dm} is unusually deep — confirm (feet entered as metres?)")
        except (TypeError, ValueError):
            errors.append("draft_m is not a number")
    margin = d.get("safety_margin_m")
    if margin is not None:
        try:
            if float(margin) < 0:
                errors.append("safety_margin_m must be >= 0")
        except (TypeError, ValueError):
            errors.append("safety_margin_m is not a number")
    ht = d.get("hull_type", "mono")
    if ht and ht not in HULL_TYPES:
        warnings.append(f"hull_type '{ht}' not in {sorted(HULL_TYPES)}")
    return errors, warnings


if __name__ == "__main__":            # validate a profile JSON: python3 -m shared.boat_profile <json>
    import json
    import sys
    if len(sys.argv) == 2:
        with open(sys.argv[1]) as f:
            doc = json.load(f)
        errs, warns = validate(doc)
        print("errors:", errs or "none")
        print("warnings:", warns or "none")
        print("safety_depth_m:", round(safety_depth_m(doc), 3))
    else:
        print("usage: python3 -m shared.boat_profile <profile.json>")
