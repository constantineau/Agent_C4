"""A boat's polar, from its own profile — the one resolver for the Lab, the Pi engine and the Orin.

The boat profile owns its speed model (#303, #304). Sources, in order:

  1. the profile's own files, named in `polar_files` (basenames, found in the boat-data dirs):
       envelope    — the polar envelope, `[(tws, twa, stw[, vmg])]`: the ORC polar SQL
                     (`polars_<boat>.sql`, INSERT tuples) or a JSON list of rows
       sail_polars — per-sail curves `{"sails": {sail: [[tws, twa, stw], ...]}}` (optional)
       crossovers  — the sail crossover model (per-TWS sail zones + toss-up overlaps, optional)
       speed_guide — the speed guide markdown (per-TWS target tables, optional; the boat's
                     sail dial reads it)
  2. the profile's inline `cert_polar` (a fleet boat added from its ORC cert): envelope only;
  3. nothing → None. Callers refuse with `NO_POLAR`. Never another boat's polar.

`resolve(profile)` returns one plain-JSON block (`SCHEMA`), the same shape the Lab freezes into the
Deploy push for the boat, so the Pi engine and the Orin read exactly what the Lab routed on:

    {schema, boat_id, source: "files"|"cert", label, version,
     envelope: [[tws, twa, stw, vmg|null]], sail_polars: {sail: [[tws, twa, stw]]},
     crossovers: {...} | null, speed_guide: {"<tws>": [{twa, btv, vmg, awa, heel, sail}]} | null}

Boat-data dirs: `BOAT_DATA_DIR` (os.pathsep-separated), else the repo's seed + knowledge dirs (a
checkout: the Orin, tests). The Pi engine resolves with no dirs at all: aboard, a polar comes from
the Deploy push or the profile's inline cert, never from files in the image.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re

SCHEMA = "c4.boat-polar/v1"
NO_POLAR = "No polar for this boat."
NO_POLAR_ABOARD = "No boat polar loaded."
FILE_KEYS = ("envelope", "sail_polars", "crossovers", "speed_guide")

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_DIRS = (os.path.join(_HERE, "..", "vps", "db", "seed"),
              os.path.join(_HERE, "..", "vps", "agent", "knowledge"))

_POLAR_RE = re.compile(
    r"\(\s*'[^']*'\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*"
    r"([0-9.]+|NULL)\s*\)"
)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


# ------------------------------------------------------------------------------------------ files

def data_dirs():
    """Where a profile's `polar_files` are looked up."""
    env = os.environ.get("BOAT_DATA_DIR")
    if env:
        return tuple(d for d in env.split(os.pathsep) if d)
    return _REPO_DIRS


def name_ok(name) -> bool:
    """A polar file name is a plain basename: no path, no dot-start (a profile is user-editable)."""
    return isinstance(name, str) and bool(_NAME_RE.match(name)) and ".." not in name


def files_errors(pf) -> list:
    """Shape errors in a profile's `polar_files` (None/absent is fine)."""
    if pf is None:
        return []
    if not isinstance(pf, dict):
        return ["bad polar_files"]
    errs = []
    for k, v in pf.items():
        if k not in FILE_KEYS:
            errs.append(f"unknown polar_files key {str(k)[:30]}")
        elif v not in (None, "") and not name_ok(v):
            errs.append(f"bad polar_files.{k}")
    return errs


def _find(name, dirs):
    if not name_ok(name):
        return None
    for d in dirs or ():
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------------------- parsers

def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _vmg(tws, twa, stw):
    return round(abs(stw * math.cos(math.radians(twa))), 2)


def parse_polar_sql(text):
    """The ORC polar SQL's INSERT tuples → [(tws, twa, stw, vmg|None)]."""
    return [(float(t), float(a), float(s), None if v == "NULL" else float(v))
            for t, a, s, v in _POLAR_RE.findall(text or "")]


def parse_envelope_rows(rows):
    """[[tws, twa, stw(, vmg)]] (JSON / an inline cert polar) → [(tws, twa, stw, vmg)]; a missing
    VMG is the speed made good up or down wind (|stw·cos twa|, as the ORC SQL carries it)."""
    out = []
    for r in rows or ():
        if not isinstance(r, (list, tuple)) or len(r) < 3:
            continue
        t, a, s = _f(r[0]), _f(r[1]), _f(r[2])
        if t is None or a is None or s is None:
            continue
        v = _f(r[3]) if len(r) > 3 and r[3] is not None else None
        out.append((t, a, s, v if v is not None else _vmg(t, a, s)))
    return out


def parse_sail_polars(obj):
    """{"sails": {sail: [[tws, twa, stw|null]]}} → {sail: [(tws, twa, stw)]} (null speeds dropped)."""
    raw = (obj or {}).get("sails", {}) if isinstance(obj, dict) else {}
    out = {}
    for s, pts in (raw or {}).items():
        rows = []
        for p in pts or ():
            if isinstance(p, (list, tuple)) and len(p) >= 3:
                t, a, v = _f(p[0]), _f(p[1]), _f(p[2])
                if t is not None and a is not None and v is not None:
                    rows.append((t, a, v))
        if rows:
            out[str(s)] = rows
    return out


def _num_cell(s):
    try:
        return float(s.strip().rstrip("°"))
    except (ValueError, AttributeError):
        return None


def parse_speed_guide(text):
    """The speed guide markdown's per-TWS tables → {"<tws>": [{twa, btv, vmg, awa, heel, sail}]},
    rows sorted by TWA. `sail` is the table's raw Sail cell (the reader maps it to its sail family)."""
    sections, tws = {}, None
    for line in (text or "").splitlines():
        m = re.match(r"##\s*TWS\s*([\d.]+)\s*kn", line)
        if m:
            tws = "%g" % float(m.group(1))
            sections[tws] = []
            continue
        if tws is None or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or not re.match(r"[\d.]+°?$", cells[0]):
            continue  # header / separator / malformed
        sections[tws].append({
            "twa": _num_cell(cells[0]), "btv": _num_cell(cells[1]), "vmg": _num_cell(cells[2]),
            "awa": _num_cell(cells[4]), "heel": _num_cell(cells[5]), "sail": cells[8],
        })
    for rows in sections.values():
        rows.sort(key=lambda r: r["twa"] if r["twa"] is not None else 0.0)
    return {k: v for k, v in sections.items() if v}


def _load_envelope(path):
    text = _read(path)
    if path.lower().endswith(".json"):
        obj = json.loads(text)
        return parse_envelope_rows(obj.get("polar") if isinstance(obj, dict) else obj)
    return parse_polar_sql(text)


# --------------------------------------------------------------------------------------- resolver

_CACHE: dict = {}
_CACHE_MAX = 16


def _version(block):
    body = {k: v for k, v in block.items() if k != "version"}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _block(boat_id, source, label, envelope, sail_polars=None, crossovers=None, speed_guide=None,
           files=None):
    b = {"schema": SCHEMA, "boat_id": boat_id, "source": source, "label": label,
         "envelope": [list(r) for r in envelope],
         "sail_polars": {s: [list(p) for p in pts] for s, pts in (sail_polars or {}).items()},
         "crossovers": crossovers or None, "speed_guide": speed_guide or None}
    if files:
        b["files"] = dict(files)
    b["version"] = _version(b)
    return b


def resolve(profile, dirs=None):
    """The boat's polar block (see the module doc), or None when it has none. Never raises on a
    missing or bad file: a profile whose envelope file can't be read has no polar."""
    p = profile if isinstance(profile, dict) else {}
    bid = p.get("boat_id")
    pf = p.get("polar_files") if isinstance(p.get("polar_files"), dict) else {}
    dirs = data_dirs() if dirs is None else tuple(dirs)
    if pf.get("envelope") and not files_errors(pf):
        paths = {k: _find(pf.get(k), dirs) for k in FILE_KEYS if pf.get(k)}
        if not paths.get("envelope"):
            return None
        try:
            stamp = tuple(sorted((k, v, os.path.getmtime(v)) for k, v in paths.items() if v))
        except OSError:
            return None
        key = ("files", bid, stamp)
        if key in _CACHE:
            return _CACHE[key]
        try:
            env = _load_envelope(paths["envelope"])
        except (OSError, ValueError, UnicodeDecodeError):
            return None
        if not env:
            return None

        def opt(k, load):
            if not paths.get(k):
                return None
            try:
                return load(_read(paths[k]))
            except (OSError, ValueError, UnicodeDecodeError):
                return None

        sp = opt("sail_polars", lambda t: parse_sail_polars(json.loads(t))) or {}
        xo = opt("crossovers", json.loads)
        sg = opt("speed_guide", parse_speed_guide)
        b = _block(bid, "files", pf["envelope"], env, sp, xo if isinstance(xo, dict) else None,
                   sg, files={k: pf[k] for k in FILE_KEYS if pf.get(k)})
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = b
        return b
    cert = p.get("cert_polar")
    if isinstance(cert, list) and cert:
        env = parse_envelope_rows(cert)
        if env:
            return _block(bid, "cert", "ORC cert", env)
    return None


# ------------------------------------------------------------------------------ carried block

def check(block, boat_id=None) -> list:
    """Shape errors in a carried polar block (the Pi refuses a Deploy push that has any)."""
    if not isinstance(block, dict):
        return ["bad polar"]
    if block.get("schema") != SCHEMA:
        return ["polar is not c4.boat-polar/v1"]
    errs = []
    if boat_id is not None and block.get("boat_id") != boat_id:
        errs.append("polar is for another boat")
    if block.get("source") not in ("files", "cert"):
        errs.append("bad polar source")
    env = block.get("envelope")
    if not isinstance(env, list) or not env or not all(
            isinstance(r, list) and len(r) == 4 and all(_f(x) is not None for x in r[:3])
            and (r[3] is None or _f(r[3]) is not None) for r in env):
        errs.append("bad polar envelope")
    sp = block.get("sail_polars")
    if not isinstance(sp, dict) or not all(
            isinstance(pts, list) and all(isinstance(q, list) and len(q) == 3
                                          and all(_f(x) is not None for x in q) for q in pts)
            for pts in sp.values()):
        errs.append("bad sail polars")
    for k in ("crossovers", "speed_guide"):
        if block.get(k) is not None and not isinstance(block.get(k), dict):
            errs.append(f"bad {k}")
    sg = block.get("speed_guide")
    if isinstance(sg, dict) and not all(
            isinstance(rows, list) and all(isinstance(r, dict) for r in rows) for rows in sg.values()):
        errs.append("bad speed_guide")
    if not errs and block.get("version") != _version(block):
        errs.append("polar version doesn't match")
    return errs


def envelope_stw(block):
    """[(tws, twa, stw)] — the optimizer's polar table."""
    return [(float(r[0]), float(r[1]), float(r[2])) for r in (block or {}).get("envelope") or ()]


def sail_curves(block):
    """{sail: [(tws, twa, stw)]} — the per-sail curves ({} = envelope routing only)."""
    return {s: [tuple(float(x) for x in q) for q in pts]
            for s, pts in ((block or {}).get("sail_polars") or {}).items()}


def summary(block):
    """Which polar, in a few fields (results say which polar they used)."""
    if not block:
        return None
    return {"boat_id": block.get("boat_id"), "source": block.get("source"),
            "label": block.get("label"), "version": block.get("version")}
