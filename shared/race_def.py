"""RaceDefinition — the structured output of C4 Performance Lab race ingestion (Lab-0).

A race's published NOR + SI + SER (+ ORC data + entry list) are distilled into ONE portable
RaceDefinition that feeds three consumers:
  - the **optimizer / navigator** — course geometry (marks/gates/finish, WGS84), zones the route
    must respect, scoring objective, and the fleet;
  - the **race checklists** — `requirements`: the COMPREHENSIVE set of things the boat must do or
    carry (safety/SER equipment, registration, navigation lights, the finish/gate procedures, ...),
    each tagged with the phase + trigger it applies at. Pre-race items are the prep checklist the
    team works through; race-time items (`deliver_to_ipad=true`) are compiled into the playbook and
    surfaced on the onboard console at the right moment (e.g. nav lights at sunset; the GPS photo +
    displaying registration/sail numbers at the finish).
  - the **rules / scoring layer** — `rules_profile` (rule modifications + scoring). The RRS-41
    carve-out (NOR §2.1(d)) is just ONE modification the race gate reads; it is not the focus —
    comprehensive requirement-checking is.

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

# Comprehensive race-requirement checklist taxonomy.
REQ_CATEGORIES = {"safety", "structural", "crew_safety", "navigation", "communications",
                  "registration", "procedure", "reporting", "environmental", "rules"}
# The phase of the regatta a requirement applies at.
REQ_PHASES = {"pre_entry", "pre_start", "start", "in_race", "at_gate", "at_finish", "post_race"}
# What surfaces a race-time requirement on the onboard console.
TRIGGER_TYPES = {"none", "time", "event", "location"}


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
    radius_nm: Optional[float] = None   # for islands/hazards: obstacle buffer radius (nm)
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
class Requirement:
    """One comprehensive race-requirement / checklist item (from NOR / SI / SER).

    Pre-race items form the prep checklist the team works through in the Lab; items with
    `deliver_to_ipad=True` are compiled into the playbook and surfaced on the onboard console at
    their trigger (e.g. nav lights at sunset; the finish procedure on the finish approach)."""
    id: str
    category: str                  # REQ_CATEGORIES
    phase: str                     # REQ_PHASES
    text: str
    trigger_type: str = "none"     # TRIGGER_TYPES — how it surfaces in-race
    trigger_detail: str = ""       # e.g. "sunset->sunrise", "finishing", "Cove Island gate"
    deliver_to_ipad: bool = False  # push to the onboard console (a race-time action item)
    critical: bool = False         # safety/DSQ-critical
    source: str = ""               # e.g. "NOR §11.3", "SER 3.3.1"


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
class TrackerConfig:
    """Public race tracker as an over-the-horizon FLEET source (YB/TracTrac-style). Whether it may be
    used ONBOARD is gated by `rules_profile.tracker_permitted` (per-race SI), NOT by this block — this
    only carries HOW to fetch it. `provider`: 'yb'(==bycmack/ybtracking)|'generic_json'|'tractrac'|
    'sample'. For `yb` (bycmack.com/tracking IS YB Tracking) set `race` (the yb.tl id, e.g.
    'bayviewmack2026') + optional `host` (default cf.yb.tl) — the GetPositions JSON endpoint is built
    from those; no field map needed (name/lat/lon/sog/cog/time are at known keys). For `generic_json`
    set `url` (the JSON/XHR endpoint behind the web UI) + optional `fields` ({name,lat,lon,sog,cog,time}
    → the feed's names) + `list_path` (dotted path to the boat list). `delay_min`: the feed's nominal
    publish delay (used for the honest 'delayed' note)."""
    provider: str = ""
    url: str = ""
    race: str = ""                                     # yb.tl race id (yb provider)
    host: str = ""                                     # yb host override (default cf.yb.tl)
    fields: dict = field(default_factory=dict)
    list_path: str = ""
    delay_min: Optional[float] = None
    note: str = ""


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
    requirements: list = field(default_factory=list)   # [Requirement] — comprehensive checklists
    rules_profile: dict = field(default_factory=dict)  # RulesProfile (rule mods + scoring)
    fleet: list = field(default_factory=list)          # [FleetEntry]
    tracker: dict = field(default_factory=dict)        # public race tracker source config (see TrackerConfig)
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
    reqs = d.get("requirements", [])
    if not reqs:
        warnings.append("no requirements/checklist items — comprehensive rules not yet captured")
    for r in reqs:
        if r.get("category") not in REQ_CATEGORIES:
            errors.append(f"requirement {r.get('id')!r}: bad category {r.get('category')!r}")
        if r.get("phase") not in REQ_PHASES:
            errors.append(f"requirement {r.get('id')!r}: bad phase {r.get('phase')!r}")
        if r.get("trigger_type", "none") not in TRIGGER_TYPES:
            errors.append(f"requirement {r.get('id')!r}: bad trigger_type {r.get('trigger_type')!r}")
        if r.get("deliver_to_ipad") and r.get("trigger_type", "none") == "none":
            warnings.append(f"requirement {r.get('id')!r} is delivered to the iPad but has no "
                            f"trigger — when should it surface?")
    rp = d.get("rules_profile", {})
    if rp.get("tracker_permitted") is None:
        warnings.append("rules_profile.tracker_permitted unset — confirm per-race (SI)")
    tr = d.get("tracker", {})
    if tr:
        prov = (tr.get("provider") or "").lower()
        _yb = prov in ("yb", "ybtracking", "yellowbrick", "bycmack")
        # a yb provider is fetchable from `race` (the id) OR `url`; others need `url`.
        fetchable = prov == "sample" or bool(tr.get("url")) or (_yb and bool(tr.get("race")))
        if not prov:
            warnings.append("tracker block has no provider — set provider + url/race (or 'sample')")
        elif not fetchable:
            warnings.append(f"tracker provider {prov!r} not fetchable — set "
                            + ("`race` (e.g. 'bayviewmack2026')" if _yb else "`url`")
                            + " — verify the live endpoint")
        if rp.get("tracker_permitted") and not fetchable:
            warnings.append("tracker is permitted but not fetchable — over-the-horizon "
                            "fleet will be empty until the endpoint/race id is set")
    return errors, warnings


def _mid(a, b):
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def course_to_marks(definition: dict, course_id: str = None):
    """Flatten a RaceDefinition course into the navigator's ordered marks: [(seq, name, lat, lon)].

    A gate → its midpoint (the nav target to pass between); the finish line → its midpoint. Marks of
    type `island` are NOT nav waypoints — they are obstacles to leave to a side, handled by the
    obstacle-avoidance layer (`app.geo`), so they are omitted here (an un-geocoded one is still
    reported as skipped so it gets reviewed). Other marks with no coordinates are skipped + returned
    so the caller can warn. Returns (marks, skipped_names, course_id). This is the homework→onboard
    link: the same marks are written to the cloud `waypoints` or the Pi marks store (OnboardSource)."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or (courses[0] if courses else None)
    if not course:
        return [], [], None
    marks, skipped, seq = [], [], 1
    start = course.get("start") or {}
    if start.get("lat") is not None:
        marks.append((seq, "Start", start["lat"], start["lon"])); seq += 1
    for m in course.get("marks", []):
        name = m.get("name", "mark")
        if m.get("type") == "island":
            if m.get("lat") is None:               # un-geocoded — still needs review
                skipped.append(name)
            continue                               # geocoded islands are obstacles, not waypoints
        if m.get("type") == "gate" and m.get("lat") is not None and m.get("lat2") is not None:
            la, lo = _mid((m["lat"], m["lon"]), (m["lat2"], m["lon2"]))
            marks.append((seq, name, la, lo)); seq += 1
        elif m.get("lat") is not None:
            marks.append((seq, name, m["lat"], m["lon"])); seq += 1
        else:
            skipped.append(name)
    pts = [p for p in ((course.get("finish") or {}).get("points") or []) if p and p.get("lat") is not None]
    if len(pts) >= 2:
        la, lo = _mid((pts[0]["lat"], pts[0]["lon"]), (pts[1]["lat"], pts[1]["lon"]))
        marks.append((seq, "Finish", la, lo))
    elif len(pts) == 1:
        marks.append((seq, "Finish", pts[0]["lat"], pts[0]["lon"]))
    return marks, skipped, course.get("id")


def course_roundings(definition: dict, course_id: str = None) -> dict:
    """Map nav-mark name → rounding side ('port'|'starboard'|'gate'|'none'), aligned to
    `course_to_marks` output. Start/Finish have no side ('none'); a gate is 'gate' (pass between).
    The optimizer uses this to leave a port/starboard mark on the correct side (it routes to the
    mark POINT; the rounding side standsoff the approach/exit to the right side). Islands are
    obstacles, not nav marks, so they're excluded here (same as `course_to_marks`) — island rounding
    side is the obstacle layer's job."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        return {}
    out = {"Start": "none", "Finish": "none"}
    for m in course.get("marks", []):
        if m.get("type") == "island":
            continue
        out[m.get("name", "mark")] = m.get("rounding", "none")
    return out


def fleet_blob(definition: dict, own: dict = None) -> dict:
    """Build the onboard fleet homework from a RaceDefinition: the competitor roster, the scoring
    flavor (ToT/ToD → corrected-time math), and the own boat's rating. This is the fleet counterpart
    of `course_to_marks` — the same homework→onboard link, loaded via `POST /fleet/load` and frozen
    at the gun. `own` (optional) = {boat, mmsi?, orc_gph?, rating?, division?} for the home boat so
    its corrected-time can be compared against the fleet; if omitted, the engine falls back to a
    neutral coefficient. Also carries the public-tracker config (over-the-horizon fleet source); its
    `permitted` flag is driven STRICTLY by `rules_profile.tracker_permitted` (default conservative —
    None/False → off), never by the tracker block itself. Returns {fleet, scoring, own, tracker}."""
    roster = []
    for e in definition.get("fleet", []) or []:
        roster.append({"boat": e.get("boat"), "division": e.get("division", ""),
                       "cls": e.get("cls", ""), "owner": e.get("owner", ""),
                       "orc_gph": e.get("orc_gph"), "rating": e.get("rating"),
                       "mmsi": e.get("mmsi")})
    rp = definition.get("rules_profile") or {}
    scoring = rp.get("scoring") or {}
    tracker = dict(definition.get("tracker") or {})
    if tracker:
        tracker["permitted"] = bool(rp.get("tracker_permitted"))   # per-race gate is authoritative
    return {"fleet": roster, "scoring": scoring, "own": own or {}, "tracker": tracker}


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        print("usage: python -m shared.race_def <race_definition.json>")
        sys.exit(2)
    data = load(path)
    errs, warns = validate(data)
    print(f"RaceDefinition: {data.get('name')} (schema {data.get('schema_version')})")
    reqs = data.get("requirements", [])
    ipad = [r for r in reqs if r.get("deliver_to_ipad")]
    print(f"  courses={len(data.get('courses', []))} divisions={len(data.get('divisions', []))} "
          f"zones={len(data.get('zones', []))} requirements={len(reqs)} "
          f"(→iPad {len(ipad)}) fleet={len(data.get('fleet', []))}")
    for w in warns:
        print(f"  WARN: {w}")
    for e in errs:
        print(f"  ERROR: {e}")
    print("OK (valid; warnings are human-review items)" if not errs else "INVALID")
    sys.exit(1 if errs else 0)
