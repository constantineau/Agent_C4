"""RaceDefinition — the structured output of C4 Performance Lab race ingestion (Lab-0).

A race's published NOR + SI (+ ORC data + entry list) are distilled into ONE portable
RaceDefinition that feeds two consumers:
  - the **optimizer / navigator** — course geometry (marks/gates/finish, WGS84), zones the route
    must respect, scoring objective, and the fleet;
  - the **RRS-41 race gate** — the per-race `rules_profile` (rule modifications, e.g. the 2026
    Bayview Mackinac NOR §2.1(d) change to RRS 41(c) that drove the three-tier architecture).

Ingestion is dual-input (auto-find from a race URL OR a pasted link / uploaded PDF) and ALWAYS
ends in a human-review step — a wrong waypoint is dangerous and coordinate formats vary. This
module is the schema + a dependency-free validator; the extraction/ingestion service is built on
top of it. See vps/lab/README.md.

Coordinates are stored as **decimal degrees, WGS84** (north/east positive), matching the engine.
NORs publish degrees-decimal-minutes; `dm_to_dd` documents the conversion used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional

SCHEMA_VERSION = "0.1"

ROUNDINGS = {"port", "starboard", "gate", "none"}
MARK_TYPES = {"start", "waypoint", "gate", "island", "buoy", "finish"}
COORDS_SOURCES = {"nor", "si", "chart", "orc", "approx", "needs_review", "si_pending"}


def dm_to_dd(deg: float, minutes: float, hemi: str) -> float:
    """Degrees + decimal-minutes + hemisphere ('N'/'S'/'E'/'W') -> signed decimal degrees."""
    dd = abs(deg) + minutes / 60.0
    return -dd if hemi.upper() in ("S", "W") else dd


@dataclass
class Mark:
    seq: int
    name: str
    type: str                      # MARK_TYPES
    rounding: str = "none"         # ROUNDINGS — side to leave the mark / gate
    lat: Optional[float] = None    # decimal degrees, WGS84 (None = not yet known / needs review)
    lon: Optional[float] = None
    lat2: Optional[float] = None   # second point for a gate / line
    lon2: Optional[float] = None
    coords_source: str = "needs_review"
    note: str = ""


@dataclass
class Finish:
    type: str                      # e.g. "virtual_gps_line"
    points: list = field(default_factory=list)   # [{name,lat,lon}, ...]
    crossing: str = ""             # e.g. "East to West"
    note: str = ""
    coords_source: str = "needs_review"


@dataclass
class Course:
    id: str
    name: str
    applies_to_divisions: list = field(default_factory=list)
    distance_nm: Optional[float] = None
    start: dict = field(default_factory=dict)     # {type, ref, lat?, lon?, coords_source}
    marks: list = field(default_factory=list)     # [Mark]
    finish: Optional[dict] = None                 # Finish
    note: str = ""


@dataclass
class Division:
    id: str
    name: str
    course_ref: str                # -> Course.id
    boat_type: str = ""


@dataclass
class Scoring:
    system: str = ""               # e.g. "ORC"
    method: str = ""               # e.g. "Single-Number Time-on-Time (ToT)"
    options: list = field(default_factory=list)
    decided: str = ""              # when/how the option is fixed (e.g. race-morning briefing)
    ref: str = ""


@dataclass
class RulesProfile:
    """Feeds the RRS-41 race gate + the rules layer."""
    rrs_edition: str = "2025-2028"
    modifications: list = field(default_factory=list)   # [{ref, rule, summary}]
    # The §2.1(d)-style carve-out that the three-tier design depends on:
    info_available_to_all_permitted: bool = False
    customized_advice_while_underway_prohibited: bool = True
    appendix_wp: bool = False       # World Sailing Appendix WP (waypoint racing) in force
    tracker_permitted: Optional[bool] = None   # official public tracker OK onboard? (per-race SI)
    scoring: dict = field(default_factory=dict)        # Scoring


@dataclass
class Zone:
    name: str
    type: str                      # "exclusion" | "tss" | "hazard"
    geometry: Optional[dict] = None
    note: str = ""


@dataclass
class FleetEntry:
    boat: str
    division: str = ""
    cls: str = ""
    owner: str = ""
    orc_gph: Optional[float] = None
    rating: Optional[float] = None
    mmsi: Optional[str] = None


@dataclass
class Provenance:
    sources: list = field(default_factory=list)        # [{label, url, retrieved}]
    si_status: str = ""
    review_status: str = "machine-extracted — NEEDS HUMAN REVIEW"
    extracted_by: str = ""


@dataclass
class RaceDefinition:
    race_id: str
    name: str
    year: int
    organizing_authority: str = ""
    start_date: str = ""           # ISO date
    start_area: str = ""
    region: str = ""
    divisions: list = field(default_factory=list)      # [Division]
    courses: list = field(default_factory=list)        # [Course]
    zones: list = field(default_factory=list)          # [Zone]
    rules_profile: dict = field(default_factory=dict)  # RulesProfile
    fleet: list = field(default_factory=list)          # [FleetEntry]
    provenance: dict = field(default_factory=dict)     # Provenance
    schema_version: str = SCHEMA_VERSION


# --- (de)serialize + validate -------------------------------------------------
def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def dump(defn: RaceDefinition, path: str) -> None:
    with open(path, "w") as f:
        json.dump(asdict(defn), f, indent=2)


def _valid_coord(lat, lon) -> bool:
    return (lat is None or -90 <= lat <= 90) and (lon is None or -180 <= lon <= 180)


def validate(d: dict) -> tuple[list, list]:
    """Return (errors, warnings). Errors must block activation; warnings flag review items
    (e.g. unknown island coords) that are expected before the human-review step signs off."""
    errors, warnings = [], []
    for key in ("race_id", "name", "year", "courses", "rules_profile"):
        if not d.get(key):
            errors.append(f"missing required field: {key}")
    course_ids = {c.get("id") for c in d.get("courses", [])}
    for div in d.get("divisions", []):
        if div.get("course_ref") not in course_ids:
            errors.append(f"division {div.get('id')!r} references unknown course "
                          f"{div.get('course_ref')!r}")
    for c in d.get("courses", []):
        if not c.get("finish"):
            warnings.append(f"course {c.get('id')!r} has no finish defined")
        for m in c.get("marks", []):
            if m.get("rounding", "none") not in ROUNDINGS:
                errors.append(f"mark {m.get('name')!r}: bad rounding {m.get('rounding')!r}")
            if m.get("type") not in MARK_TYPES:
                errors.append(f"mark {m.get('name')!r}: bad type {m.get('type')!r}")
            if not _valid_coord(m.get("lat"), m.get("lon")):
                errors.append(f"mark {m.get('name')!r}: coord out of range")
            if m.get("lat") is None and m.get("type") in ("gate", "waypoint", "buoy"):
                warnings.append(f"mark {m.get('name')!r} ({m.get('type')}) has no coordinates "
                                f"— needs review")
            if m.get("coords_source") == "needs_review":
                warnings.append(f"mark {m.get('name')!r}: coords need review")
    rp = d.get("rules_profile", {})
    if rp.get("tracker_permitted") is None:
        warnings.append("rules_profile.tracker_permitted unset — confirm per-race (SI)")
    return errors, warnings


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("usage: python -m shared.race_def <race_definition.json>")
        sys.exit(2)
    data = load(path)
    errs, warns = validate(data)
    print(f"RaceDefinition: {data.get('name')} (schema {data.get('schema_version')})")
    print(f"  courses={len(data.get('courses', []))} divisions={len(data.get('divisions', []))} "
          f"zones={len(data.get('zones', []))} fleet={len(data.get('fleet', []))}")
    for w in warns:
        print(f"  WARN: {w}")
    for e in errs:
        print(f"  ERROR: {e}")
    print("OK (valid; warnings are human-review items)" if not errs else "INVALID")
    sys.exit(1 if errs else 0)
