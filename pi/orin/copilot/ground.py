"""Number grounding for LLM free text — the one post-check every LLM text path shares.

The engine computes every number the crew sees. The LLM may only REPHRASE. So before any LLM text
replaces deterministic text, every number in it must already appear in the deterministic input it
was given; one stray number (a changed CPA, a made-up wind speed) and the caller keeps its own
deterministic text instead. Pure, stdlib only.

Numbers are compared as values ("12" == "12.0"), sign-blind ("−60 min" == "60"), and only as
standalone tokens: a digit glued to a letter is a name, not a number ("J1", "A2", "P3").
"""
import json
import re

# A standalone number: not preceded by a letter/digit/dot (so "J1" and the "5" in "1.5" are not
# picked up on their own), optional thousands separators and decimals. A leading-dot decimal
# (".9 nm") counts as 0.9 — a dot after a digit/letter ("v1.9", "1.5") is not a leading dot.
# The unit right after it, if any, rides along: "0.6 nm" and "9 min" are different facts from
# "9 nm" and "0.6 min", and "-60 min" does not ground "60 kts" or "60-knot gusts". A compound
# unit is read whole ("60 m/s" is not "60 m", "3 kn/h" is not "3 kn").
_NUM = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
                  r"(?:[\s-]*((?:°[CF]\b|°|%|[A-Za-z]+)(?:/(?:°|[A-Za-z]+))*))?")
# A name with digits glued to letters ("J1", "M2", "TWS30"). Names are not numbers, but an
# invented one is still invented: every name in the text must appear in a source.
_NAME = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]+\d[A-Za-z0-9]*")

_UNITS = {
    "kt": "kt", "kts": "kt", "kn": "kt", "kns": "kt", "knot": "kt", "knots": "kt",
    "nm": "nm", "nmi": "nm", "mi": "mi", "mile": "mi", "miles": "mi",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "°": "deg", "deg": "deg", "degree": "deg", "degrees": "deg",
    "v": "V", "volt": "V", "volts": "V", "a": "A", "amp": "A", "amps": "A",
    "%": "%", "pct": "%", "percent": "%",
    # units the engine never uses still count as units: "60 mph" must not borrow a bare 60
    "mph": "mph", "kph": "km/h", "kmh": "km/h", "km": "km", "ft": "ft", "feet": "ft",
    "mb": "hPa", "mbar": "hPa", "hpa": "hPa", "c": "C", "f": "F", "°c": "C", "°f": "F", "nmph": "kt",
}
for _k in list(_UNITS):                         # "knot", "60-knot", "knots" alike
    if len(_k) > 2:
        _UNITS.setdefault(_k.rstrip("s"), _UNITS[_k])


def _unit(tok):
    """A unit token → its canonical unit; None for a plain word ("12 to 18"). A compound is
    normalized part by part ("kts/hr" → "kt/h") and an unknown part stays itself, so an unknown
    compound is its own unit, never a prefix of a known one."""
    tok = (tok or "").lower()
    if "/" not in tok:
        return _UNITS.get(tok)
    return "/".join(_UNITS.get(p, p) for p in tok.split("/"))


# Raw engine JSON has no units ({"tws": 18, "twd": 60}), so a value's field name gives it one.
# "1" is a known unit-less quantity (an index, a count): it grounds the bare number only.
_FIELDS = {}
for _u, _ks in {
    "kt": "tws aws sog stw vmg vmc boatspeed kn target_stw target_vmg dtws",
    "deg": "twd twa awa hdg heading heading_true heading_mag cog bearing heel pitch rudder_angle "
           "lat lon latitude longitude now_twd ref_twd base_twd plan_twd obs_twd twa_min twa_max "
           "lo_twa hi_twa beat_twa run_twa at_twa twa_abs dtwd ref_bearing seg_bearing deg",
    "nm": "dtw dtf cpa xte distance nm leverage",
    "min": "ttg tcpa",
    "V": "bank bank_voltage volts volts_min volts_last volts_decided",
    "deg/s": "rate_of_turn", "C": "water_temp", "m": "depth",
    "1": "fatigue fatigue_index index mmsi own_mmsi count n rank score samples seq helm_factor "
         "confidence_value",
}.items():
    _FIELDS.update(dict.fromkeys(_ks.split(), _u))
# Suffixes, longest first: "cpa_nm", "tcpa_min", "tws_rate_kn_per_hr".
_SUFFIX = [("_kn_per_hr", "kt/h"), ("_kn_per_h", "kt/h"), ("_deg_per_hr", "deg/h"),
           ("_deg_per_h", "deg/h"), ("_v_per_h", "V/h"), ("_seconds", "s"), ("_minutes", "min"),
           ("_hours", "h"), ("_kts", "kt"), ("_kn", "kt"), ("_kt", "kt"), ("_deg", "deg"),
           ("_nm", "nm"), ("_min", "min"), ("_pct", "%"), ("_hr", "h"), ("_s", "s"), ("_h", "h"),
           ("_m", "m"), ("_v", "V")]
# Fields that hold their parent's quantity: {"tws": {"unit": "kn", "preferred": {"value": 18}}}.
_VALUE_KEYS = {"value", "now", "mean", "min", "max", "avg", "median", "last", None}
_ANY = "*"                                      # an unknown field: no unit known, grounds any


def _field_unit(key):
    if not isinstance(key, str):
        return None
    k = key.lower()
    if k in _FIELDS:
        return _FIELDS[k]
    if k.startswith(("n_", "count_")):
        return "1"
    return next((u for suf, u in _SUFFIX if k.endswith(suf)), None)


def _walk(obj, out, key=None, ctx=None):
    """Quantities in engine data: a number takes its field's unit, else (for value-like fields)
    the unit of the field or `unit` it sits under, else it is unknown (_ANY). Text is parsed."""
    if isinstance(obj, dict):
        if isinstance(obj.get("unit"), str):
            ctx = _unit(obj["unit"]) or ctx
        for k, v in obj.items():
            _walk(v, out, k, _field_unit(k) or ctx)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk(v, out, key, ctx)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        u = _field_unit(key) or (ctx if key in _VALUE_KEYS else None)
        out.add((round(float(abs(obj)), 6), u or _ANY))
    elif isinstance(obj, str):
        out |= quantities(obj)


def _text(text):
    if text is None:
        return ""
    return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)


def quantities(text) -> set[tuple[float, str | None]]:
    """Every number in `text` as (value, unit). In a string the unit is the one written after it
    (None if none); dicts/lists are walked as engine data (see _walk; unknown fields are "*")."""
    out = set()
    if text is None:
        return out
    if not isinstance(text, str):
        _walk(text, out)
        return out
    for m in _NUM.finditer(text):
        try:
            v = round(float(m.group(1).replace(",", "")), 6)
        except ValueError:
            continue
        out.add((v, _unit(m.group(2))))
    return out


def numbers(text) -> set[float]:
    """Every number's value in `text` (strings or engine data)."""
    return {v for v, _ in quantities(text)}


def names(text) -> set[str]:
    """Every digit-bearing name ("J1", "TWS30"), upper-cased."""
    return {m.group(0).upper() for m in _NAME.finditer(_text(text))}


def _sources(sources):
    have = set()
    for s in sources:
        have |= quantities(s)
    return have


def ungrounded_numbers(text, *sources) -> set[float]:
    """The numbers in `text` that no source backs. A number with a unit needs the same value
    with the same unit, or the same value from an engine field whose unit isn't known; a bare
    number needs the same value in any unit. A bare number in source TEXT grounds no unit."""
    have = _sources(sources)
    values = {v for v, _ in have}
    out = set()
    for v, u in quantities(text):
        ok = (v, u) in have or (v, _ANY) in have if u else v in values
        if not ok:
            out.add(v)
    return out


def ungrounded(text, *sources) -> list[str]:
    """Human-readable list of everything in `text` the sources don't back (numbers and names)."""
    have_names = set()
    for s in sources:
        have_names |= names(s)
    return ([f"{v:g}" for v in sorted(ungrounded_numbers(text, *sources))]
            + sorted(names(text) - have_names))


def grounded(text, *sources) -> bool:
    """True when every number (with its unit) and every digit-bearing name in `text` appears in at
    least one of `sources`."""
    return not ungrounded(text, *sources)


def keeps_numbers(text, required) -> bool:
    """True when `text` still carries every number in `required` (the LLM line didn't drop the
    top item's facts — a CPA, a distance, a time)."""
    return numbers(required) <= numbers(text)
