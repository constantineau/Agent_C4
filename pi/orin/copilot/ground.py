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
# "9 nm" and "0.6 min", and "-60 min" does not ground "60 kts" or "60-knot gusts".
_NUM = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)"
                  r"(?:[\s-]?(°|%|[A-Za-z]+(?:/h)?))?")
# A name with digits glued to letters ("J1", "M2", "TWS30"). Names are not numbers, but an
# invented one is still invented: every name in the text must appear in a source.
_NAME = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]+\d[A-Za-z0-9]*")

_UNITS = {
    "kt": "kt", "kts": "kt", "knot": "kt", "knots": "kt",
    "nm": "nm", "nmi": "nm", "mi": "nm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "°": "deg", "deg": "deg", "degree": "deg", "degrees": "deg",
    "v": "V", "volt": "V", "volts": "V", "a": "A", "amp": "A", "amps": "A",
    "%": "%", "pct": "%", "percent": "%",
    # units the engine never uses still count as units: "60 mph" must not borrow a bare 60
    "mph": "mph", "kph": "kph", "kmh": "kph", "km/h": "kph", "km": "km", "ft": "ft", "feet": "ft",
    "mb": "hPa", "mbar": "hPa", "hpa": "hPa", "c": "C", "f": "F", "nmph": "kt",
}
for _k in list(_UNITS):                         # "knot", "60-knot", "knots" alike
    if len(_k) > 2:
        _UNITS.setdefault(_k.rstrip("s"), _UNITS[_k])


def _text(text):
    if text is None:
        return ""
    return text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)


def quantities(text) -> set[tuple[float, str | None]]:
    """Every standalone number in `text` as (value, unit or None). Dicts/lists are JSON-dumped."""
    out = set()
    for m in _NUM.finditer(_text(text)):
        try:
            v = round(float(m.group(1).replace(",", "")), 6)
        except ValueError:
            continue
        out.add((v, _UNITS.get((m.group(2) or "").lower())))
    return out


def numbers(text) -> set[float]:
    """Every standalone number's value in `text` (dicts/lists are JSON-dumped first)."""
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
    with the same unit, or the same value with no unit (raw engine data like {"tws": 12.4});
    a bare number needs the same value in any unit."""
    have = _sources(sources)
    values = {v for v, _ in have}
    out = set()
    for v, u in quantities(text):
        ok = (v, u) in have or (v, None) in have if u else v in values
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
