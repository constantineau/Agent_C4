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
import re
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
# Handicap systems a race can score on (rules_profile.scoring.rating_system). Only ORC has a
# rating feed today; the others are named so a race can say so instead of being read as ORC.
RATING_SYSTEMS = {"ORC", "PHRF", "IRC", "ONE_DESIGN"}


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
    # Which handicap number the race scores on (#311). Read by the Lab fleet import and the in-race
    # fleet model; nothing guesses it from the race's name. See `rating_spec` / `rating_countries`.
    rating_system: str = ""        # RATING_SYSTEMS, "" = not stated yet
    rating_column: str = ""        # ORC: certificate column for the ToT number (e.g. "TMF_Inshore")
    rating_column_tod: str = ""    # ORC: matching ToD column, "" if none
    course_columns: list = field(default_factory=list)   # [{course, column, column_tod}] per course
    rating_countries: list = field(default_factory=list)  # ORC national authorities to search


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
    sail: str = ""                                    # sail number — entry-list ID + ORC-cert match key
    orc_gph: Optional[float] = None
    rating: Optional[float] = None
    tod_allowance: Optional[float] = None             # ORC ToD allowance (s/nm) off the race's ToD
                                                      # column; None = use GPH
    rating_col: Optional[str] = None                  # column an imported rating was read off
    tod_col: Optional[str] = None                     # column an imported tod_allowance came from
    mmsi: Optional[str] = None
    source: str = ""                                  # how this entry/handicap was sourced (yb/orc/manual)


@dataclass
class TrackerConfig:
    """Public race tracker as an over-the-horizon FLEET source (YB/TracTrac-style). Whether it may be
    used ONBOARD is gated by `rules_profile.tracker_permitted` (per-race SI), NOT by this block — this
    only carries HOW to fetch it. `provider`: 'yb'(==bycmack/ybtracking)|'generic_json'|'tractrac'|
    'sample'|'file' (== upload/gpx/csv: no feed; the fleet's tracks are uploaded after the race —
    retro, debrief and the entry list read them, see vps/lab/app/trackers.py). For `yb` (bycmack.com/tracking IS YB Tracking) set `race` (the yb.tl id, e.g.
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
    ingest_notes: list = field(default_factory=list)   # [{text, acknowledged}] — what ingest missed
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
    timezone: str = ""             # IANA tz name of the race venue (e.g. "America/Detroit");
                                   # needed for any local gun time (#312). "" = gun times as epochs only.
    divisions: list = field(default_factory=list)      # [Division]
    courses: list = field(default_factory=list)        # [Course]
    zones: list = field(default_factory=list)          # [Zone]
    requirements: list = field(default_factory=list)   # [Requirement] — comprehensive checklists
    rules_profile: dict = field(default_factory=dict)  # RulesProfile (rule mods + scoring)
    fleet: list = field(default_factory=list)          # [FleetEntry]
    tracker: dict = field(default_factory=dict)        # public race tracker source config (see TrackerConfig)
    provenance: dict = field(default_factory=dict)     # Provenance
    schema_version: str = SCHEMA_VERSION
    venue_country: str = ""        # start venue's country, World Sailing 3-letter code (e.g. "GBR");
                                   # seeds the ORC country list when the scoring block names none


# --- (de)serialize + validate -------------------------------------------------
def load(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def dump(defn: RaceDefinition, path: str) -> None:
    with open(path, "w") as f:
        json.dump(asdict(defn), f, indent=2)


def _valid_coord(lat, lon) -> bool:
    def ok(v, lim):
        return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool) and -lim <= v <= lim)
    return ok(lat, 90) and ok(lon, 180)


def validate(d: dict) -> tuple[list, list]:
    """Return (errors, warnings). Errors must block activation; warnings flag review items
    (e.g. unknown island coords) that are expected before the human-review step signs off."""
    errors, warnings = [], []
    # What ingestion could NOT read (a link that failed, a trimmed or text-only document): kept in
    # provenance.ingest_notes and listed first until a person dismisses it (acknowledged=true),
    # so a saved draft can't look complete when it was built on part of the documents.
    for n in (d.get("provenance") or {}).get("ingest_notes") or []:
        if isinstance(n, dict) and n.get("text") and not n.get("acknowledged"):
            warnings.append(f"Ingest: {n['text']}")
    for key in ("race_id", "name", "year", "courses", "rules_profile"):
        if not d.get(key):
            errors.append(f"missing required field: {key}")
    _tz_err, _tz_warn = timezone_issues(d)
    errors += _tz_err
    warnings += _tz_warn
    course_ids = {c.get("id") for c in d.get("courses", [])}
    for div in d.get("divisions", []):
        if div.get("course_ref") not in course_ids:
            errors.append(f"division {div.get('id')!r} references unknown course "
                          f"{div.get('course_ref')!r}")
    for c in d.get("courses", []):
        fin = c.get("finish")
        if fin is None:
            warnings.append(f"course {c.get('id')!r} has no finish defined")
        elif not isinstance(fin, dict) or not isinstance(fin.get("points", []), (list, type(None))):
            errors.append(f"course {c.get('id')!r}: finish is malformed (want {{points: [...]}})")
        else:
            fpts = fin.get("points") or []
            if len(fpts) > 2:
                errors.append(f"course {c.get('id')!r}: finish has {len(fpts)} points (1 mark or a 2-point line)")
            for k, p in enumerate(fpts):
                if not isinstance(p, dict):
                    errors.append(f"course {c.get('id')!r}: finish point {k + 1} is malformed")
                    continue
                if not _valid_coord(p.get("lat"), p.get("lon")):
                    errors.append(f"course {c.get('id')!r}: finish point {k + 1} coord out of range")
                if p.get("lat") is None or p.get("lon") is None:
                    warnings.append(f"course {c.get('id')!r}: finish point {k + 1} has no coordinates "
                                    f"— needs review")
            if not fpts:
                warnings.append(f"course {c.get('id')!r}: finish has no points — needs review")
            if fin.get("coords_source") in ("needs_review", "si_pending"):
                warnings.append(f"course {c.get('id')!r}: finish coords need review")
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
    warnings += rating_warnings(d)
    if rp.get("tracker_permitted") is None:
        warnings.append("rules_profile.tracker_permitted unset — confirm per-race (SI)")
    tr = d.get("tracker", {})
    if tr:
        prov = (tr.get("provider") or "").lower()
        _yb = prov in ("yb", "ybtracking", "yellowbrick", "bycmack")
        # a yb provider is fetchable from `race` (the id) OR `url`; others need `url`.
        fetchable = prov == "sample" or bool(tr.get("url")) or (_yb and bool(tr.get("race")))
        # a fleet file (#310) is uploaded after the race, so there is nothing to fetch
        _file = prov in ("file", "upload", "gpx", "csv")
        if not prov:
            warnings.append("tracker block has no provider — set provider + url/race (or 'sample')")
        elif not fetchable and not _file:
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
    fin = course.get("finish")
    raw = (fin.get("points") if isinstance(fin, dict) else None) or []
    raw = raw if isinstance(raw, list) else []
    pts = [p for p in raw if isinstance(p, dict) and p.get("lat") is not None and p.get("lon") is not None]
    if fin is not None and len(pts) < max(len(raw), 1):
        skipped.append("Finish (incomplete coords)")   # half-filled / empty finish: say so, don't hide it
    if len(pts) >= 2:
        la, lo = _mid((pts[0]["lat"], pts[0]["lon"]), (pts[1]["lat"], pts[1]["lon"]))
        marks.append((seq, "Finish", la, lo))
    elif len(pts) == 1:
        marks.append((seq, "Finish", pts[0]["lat"], pts[0]["lon"]))
    return marks, skipped, course.get("id")


def course_obstacles(definition: dict, course_id: str = None, default_island_nm: float = 0.5) -> dict:
    """A COMPACT, race-scoped obstacle set for onboard use — the island marks as buffer disks
    (lat/lon/radius_nm) plus the course's exclusion/hazard zones. This is the homework the cloud
    obstacle layer (`app.geo`, GSHHG/ENC — too big for the Pi) distils down to what the ONBOARD
    re-optimizer needs: the race-critical islands (the durable lesson — the per-race island-disk layer
    is what guarantees the small obstacles a coarse coastline misses) frozen into the bundle at prep
    time. Returns {'islands': [{name,lat,lon,radius_nm}], 'zones': [{name,type,geometry}]}; an empty
    set if the course has none (the onboard router then routes open-water)."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or (courses[0] if courses else None)
    if not course:
        return {"islands": [], "zones": []}
    islands = [{"name": m.get("name", "island"), "lat": m["lat"], "lon": m["lon"],
                "radius_nm": m.get("radius_nm") or default_island_nm}
               for m in course.get("marks", [])
               if m.get("type") == "island" and m.get("lat") is not None and m.get("lon") is not None]
    zones = [{"name": z.get("name", "zone"), "type": z.get("type", "exclusion"),
              "geometry": z.get("geometry")}
             for z in (definition.get("zones") or []) if z.get("geometry")]
    return {"islands": islands, "zones": zones}


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


def marks_with_side(definition: dict, course_id: str = None):
    """Ordered [{name, type, side}] for every course mark the crew must leave on a specific hand — nav
    marks AND island marks with rounding 'port'/'starboard' (and gates, which are passed between), in
    course order. Unlike `course_roundings` (nav marks only, for the optimizer's standoff) this INCLUDES
    islands, so it's the crew-facing roundings summary: 'leave Duck Islands to starboard, Bois Blanc to
    port, …'. Marks with rounding 'none' (start/finish/free marks) are omitted."""
    courses = definition.get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    if not course:
        return []
    out = []
    for m in course.get("marks", []):
        side = m.get("rounding", "none")
        if side in ("port", "starboard", "gate"):
            out.append({"name": m.get("name", "mark"), "type": m.get("type", "mark"), "side": side})
    return out


# --- which handicap number the race scores on (#311) ----------------------------
_SAIL_PREFIX = re.compile(r"^\s*([A-Za-z]{3})[\s\-]*\d")


def rating_system(scoring: dict) -> str:
    """The race's rating system ('ORC' | 'PHRF' | 'IRC' | 'ONE_DESIGN') or '' when not stated.
    The explicit `rating_system` field wins; else a system/method text that names exactly one."""
    sc = scoring or {}
    s = re.sub(r"[\s\-]+", "_", str(sc.get("rating_system") or "").strip().upper())
    if s:
        return s
    txt = f"{sc.get('system') or ''} {sc.get('method') or ''}".upper()
    found = [n for n in ("ORC", "PHRF", "IRC") if re.search(rf"\b{n}\b", txt)]
    if re.search(r"\bONE[\s\-]?DESIGN\b", txt):
        found.append("ONE_DESIGN")
    return found[0] if len(found) == 1 else ""


def _course_for(definition: dict, course_id: str = None):
    """The course a rating is read for: the one asked, else our division's course."""
    if course_id:
        return course_id
    return course_of_division(definition, ((definition or {}).get("own") or {}).get("division"))


def rating_spec(definition: dict, course_id: str = None, own_course: bool = True) -> dict:
    """Which certificate number the race scores on: {system, column, column_tod, course}.
    `column` is None when the race does not say: callers must then ask, never guess one.
    A per-course column (scoring.course_columns) wins over the race-wide `rating_column`; with no
    course given, our division's course is used, or the only per-course column there is."""
    sc = (((definition or {}).get("rules_profile") or {}).get("scoring")) or {}
    rows = [r for r in (sc.get("course_columns") or [])
            if isinstance(r, dict) and (r.get("column") or r.get("column_tod"))]   # a ToD-only row counts
    cid = _course_for(definition, course_id) if own_course else course_id
    row = next((r for r in rows if cid and str(r.get("course")) == str(cid)), None)
    # no course known: the per-course column stands in only when EVERY course of the race uses it
    # (a race listing courses that have no row of their own doesn't count)
    course_ids = {str(c.get("id")) for c in (definition or {}).get("courses") or []
                  if isinstance(c, dict) and c.get("id")}
    covers = course_ids <= {str(r.get("course")) for r in rows}
    if (row is None and not cid and covers
            and len({(r.get("column") or "", r.get("column_tod") or "") for r in rows}) == 1):
        row = rows[0]
    col = (row or {}).get("column") or sc.get("rating_column") or None
    tod = ((row or {}).get("column_tod") if row else sc.get("rating_column_tod")) or None
    return {"system": rating_system(sc), "column": col, "column_tod": tod,
            "course": (row or {}).get("course") or cid}


def course_of_division(definition: dict, division) -> Optional[str]:
    """The course a division sails (divisions[].course_ref), or None when not listed."""
    div = str(division or "").strip()
    for dv in (definition or {}).get("divisions") or []:
        if div and str(dv.get("id") or "").strip() == div:
            return dv.get("course_ref")
    return None


def rating_spec_for(definition: dict, entry: dict, course_id: str = None) -> dict:
    """`rating_spec` for one boat: the course asked, else the boat's division's course. A boat whose
    division the race doesn't list never inherits OUR course: it gets the race-wide column (or the
    only per-course column, when every course uses the same one), else unknown. The Lab import, the
    save check and the boat blob all use this, so they agree on every boat's column."""
    course = course_id or course_of_division(definition, (entry or {}).get("division"))
    return rating_spec(definition, course, own_course=False)


# Column tags (#311). An imported number carries the certificate column it was read off
# (`rating_col` / `tod_col`); a number typed in the Lab carries MANUAL_TAG and is never cleared.
# A number with NO tag on an entry whose `source` says it came from ORC is a LEGACY import (before
# tags existed): its column is unknown, so it is cleared unless a migration tags it first
# (`tag_legacy`). An untagged number on any other entry was typed before tags existed: kept.
MANUAL_TAG = "manual"
_LEGACY = object()


def _imported(e) -> bool:
    return "orc" in str((e or {}).get("source") or "").lower()


def stale_numbers(e: dict, want_tot, want_tod) -> bool:
    """Clear `rating` / `tod_allowance` on one entry when they were imported off a column other
    than the one wanted now (see the tag rules above). Returns True when a number was cleared."""
    hit = False
    for num, tag, want in (("rating", "rating_col", want_tot), ("tod_allowance", "tod_col", want_tod)):
        if tag in e:
            have = e.get(tag)
        elif num == "rating" and _imported(e) and e.get(num) is not None:
            have = _LEGACY
        else:
            continue
        if have == MANUAL_TAG or (have is not _LEGACY and (have or None) == (want or None)):
            continue
        hit = hit or e.get(num) is not None
        e[num] = None
        e.pop(tag, None)
    return hit


def tag_legacy(definition: dict, column: str) -> int:
    """Migration: tag every legacy ORC import's rating (no tag, ORC source) with the column it is
    known to have been read off. Returns how many were tagged."""
    n = 0
    for e in (definition or {}).get("fleet") or []:
        if (isinstance(e, dict) and "rating_col" not in e and _imported(e)
                and e.get("rating") is not None):
            e["rating_col"] = column
            n += 1
    return n


def clear_stale_ratings(definition: dict) -> dict:
    """On save and in the migration (#311): clear any imported rating / ToD allowance that is not
    off the column its boat's course scores on now (tag rules above). Mutates `definition`;
    returns {"boats": [names], "own": bool}."""
    def _fix(e):
        spec = rating_spec_for(definition, e)
        return stale_numbers(e, spec["column"], spec["column_tod"])
    boats = [str(e.get("boat") or "?") for e in (definition.get("fleet") or [])
             if isinstance(e, dict) and _fix(e)]
    own = definition.get("own")
    return {"boats": boats, "own": bool(isinstance(own, dict) and _fix(own))}


def sail_prefix(sail) -> str:
    """'GBR 1234' / 'usa-60' → 'GBR' / 'USA'; '' when the sail number has no country prefix."""
    m = _SAIL_PREFIX.match(str(sail or ""))
    return m.group(1).upper() if m else ""


def rating_countries(definition: dict, entries: list = None, limit: int = 6) -> list:
    """National authorities to search for certificates. The scoring block's list wins; else the
    start venue's country, then entrants' sail-number prefixes, most common first."""
    d = definition or {}
    sc = ((d.get("rules_profile") or {}).get("scoring")) or {}
    given = sc.get("rating_countries") or []
    if isinstance(given, str):
        given = given.split(",")
    out = [str(c).strip().upper() for c in given if str(c).strip()]
    if out:
        return out
    venue = str(d.get("venue_country") or "").strip().upper()
    if venue:
        out.append(venue)
    counts = {}
    for e in entries if entries is not None else (d.get("fleet") or []):
        p = sail_prefix((e or {}).get("sail"))
        if p:
            counts[p] = counts.get(p, 0) + 1
    for p, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        if p not in out:
            out.append(p)
    return out[:limit]


def rating_warnings(d: dict) -> list:
    """Race-review flags for the rating fields: a fleet with no system, or ORC with no column."""
    sc = ((d.get("rules_profile") or {}).get("scoring")) or {}
    system = rating_system(sc)
    out = []
    if system and system not in RATING_SYSTEMS:
        out.append(f"scoring.rating_system {system!r} unknown (ORC, PHRF, IRC, ONE_DESIGN)")
    elif system == "ORC":
        cols = [r for r in (sc.get("course_columns") or [])
                if isinstance(r, dict) and (r.get("column") or r.get("column_tod"))]
        if not sc.get("rating_column") and not cols:
            out.append("Rating column unknown: pick one (scoring)")
    elif not system and d.get("fleet"):
        out.append("Rating system unknown: set it in scoring")
    # boats whose division the race doesn't list can't be put on a course: with per-course
    # columns they fall back to the race-wide one (or none), so say how many
    per = {(r.get("column") or "", r.get("column_tod") or "")
           for r in (sc.get("course_columns") or [])
           if isinstance(r, dict) and (r.get("column") or r.get("column_tod"))}
    if len(per) > 1:
        n = sum(1 for e in d.get("fleet") or []
                if isinstance(e, dict) and not course_of_division(d, e.get("division")))
        if n:
            out.append(f"{n} boat{'s' if n != 1 else ''} in a division the race doesn't list: "
                       + ("race-wide rating column used" if sc.get("rating_column")
                          else "rating column unknown"))
    return out


_PROSE_KEYS = frozenset({"note", "notes", "ref", "coords_source", "source"})


def _no_prose(v):
    if isinstance(v, dict):
        return {k: _no_prose(x) for k, x in v.items() if k not in _PROSE_KEYS}
    if isinstance(v, list):
        return [_no_prose(x) for x in v]
    return v


def course_hash(definition: dict, course_id: str = None) -> str:
    """What a playbook was routed on (#150): the course (start, marks, rounding sides, islands,
    finish) and the race's zones, minus the prose (notes, SI refs). Frozen into the bundle at
    synthesis; Deploy refuses a push when today's race hashes differently. Same course pick as
    `course_to_marks`."""
    import hashlib
    courses = (definition or {}).get("courses", []) or []
    course = next((c for c in courses if c.get("id") == course_id), None) or \
        (courses[0] if courses else None)
    payload = json.dumps(_no_prose({"course": course, "zones": (definition or {}).get("zones") or []}),
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def valid_timezone(name) -> bool:
    """True when `name` is an IANA time zone this host knows (e.g. "Europe/London")."""
    if not name or not isinstance(name, str):
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name.strip())
        return True
    except Exception:
        return False


def _is_local_time(v) -> bool:
    """A gun/start value that needs the venue time zone to mean anything: an ISO datetime string
    with no offset. Epoch numbers and strings with an offset (or Z) stand on their own."""
    if v is None or isinstance(v, (int, float)):
        return False
    s = str(v).strip()
    try:
        float(s)
        return False
    except ValueError:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).tzinfo is None
    except ValueError:
        return False


def timezone_issues(d: dict) -> tuple[list, list]:
    """(errors, warnings) for the venue time zone (#312). A local gun time with no valid time zone
    is an error: guessing UTC starts the route hours off."""
    errors, warnings = [], []
    tz = (d or {}).get("timezone") or ""
    ok = valid_timezone(tz)
    if tz and not ok:
        errors.append(f"time zone {tz!r} is not a known zone (want e.g. 'Europe/London')")
    if not ok:
        local = [k for k, v in ((d or {}).get("division_starts") or {}).items() if _is_local_time(v)]
        if local:
            errors.append(f"gun times for {', '.join(map(str, local))} are local but the race has "
                          f"no valid time zone — set one")
        elif not tz and (d or {}).get("start_date"):
            warnings.append("no time zone set — set the venue's time zone")
    return errors, warnings


def _gun_epoch(v, tz_name=""):
    """A division gun as epoch seconds. Accepts an epoch number (the Lab guns card), an ISO
    datetime with an offset, or an ISO local datetime (the SI extraction) read in the race venue's
    time zone. A local time with no valid zone is None, never a UTC guess (#312); `validate` says
    so in race review."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            if not valid_timezone(tz_name):
                return None
            from zoneinfo import ZoneInfo
            dt = dt.replace(tzinfo=ZoneInfo(tz_name.strip()))
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def fleet_blob(definition: dict, own: dict = None) -> dict:
    """Build the onboard fleet homework from a RaceDefinition: the competitor roster, the scoring
    flavor (ToT/ToD → corrected-time math), and the own boat's rating. This is the fleet counterpart
    of `course_to_marks` — the same homework→onboard link, loaded via `POST /fleet/load` and frozen
    at the gun. `own` (optional) = {boat, mmsi?, orc_gph?, rating?, division?} for the home boat so
    its corrected-time can be compared against the fleet; if omitted, the engine falls back to a
    neutral coefficient. Also carries the public-tracker config (over-the-horizon fleet source); its
    `permitted` flag is driven STRICTLY by `rules_profile.tracker_permitted` (default conservative —
    None/False → off), never by the tracker block itself. Returns {fleet, scoring, own, tracker}."""
    # last line of defence (#311): a rating imported off another column than its boat's course
    # scores on now (or a legacy import of unknown column) never reaches the boat, whatever path
    # wrote the file. Works on a copy; `stale_ratings` names the boats so the Lab can say so.
    definition = json.loads(json.dumps(definition or {}))
    if own:                       # an explicit own entry is checked like the saved one (#311)
        definition["own"] = json.loads(json.dumps(own))
    stale = clear_stale_ratings(definition)
    roster = []
    for e in definition.get("fleet", []) or []:
        spec = rating_spec_for(definition, e)      # per boat: its course may or may not name columns
        roster.append({"boat": e.get("boat"), "division": e.get("division", ""),
                       "rating_col": spec["column"], "tod_col": spec["column_tod"],
                       "cls": e.get("cls", ""), "owner": e.get("owner", ""),
                       "sail": e.get("sail", ""),
                       "orc_gph": e.get("orc_gph"), "rating": e.get("rating"),
                       "tod_allowance": e.get("tod_allowance"),
                       "mmsi": e.get("mmsi")})
    rp = definition.get("rules_profile") or {}
    scoring = rp.get("scoring") or {}
    tracker = dict(definition.get("tracker") or {})
    if tracker:
        tracker["permitted"] = bool(rp.get("tracker_permitted"))   # per-race gate is authoritative
    # per-division GUN TIMES (epoch s), entered in the Lab from the SIs — the elapsed-time term
    # that turns the onboard corrected deltas from remaining-race into FULL-RACE best estimates
    division_starts = {}
    tz_name = definition.get("timezone") or ""
    for k, v in (definition.get("division_starts") or {}).items():
        e = _gun_epoch(v, tz_name)
        if e is not None:
            division_starts[str(k)] = e
    own_out = dict(definition.get("own") or {})
    if own_out:
        ospec = rating_spec_for(definition, own_out)
        own_out.update(rating_col=ospec["column"], tod_col=ospec["column_tod"])
    out = {"fleet": roster, "scoring": scoring, "own": own_out,
           "tracker": tracker, "division_starts": division_starts}
    if stale["boats"] or stale["own"]:
        out["stale_ratings"] = stale["boats"] + (["(our boat)"] if stale["own"] else [])
    return out


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
