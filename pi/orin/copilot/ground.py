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
_NUM = re.compile(r"(?<![A-Za-z0-9_.])(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)")


def numbers(text) -> set[float]:
    """Every standalone number in `text` (dicts/lists are JSON-dumped first)."""
    if text is None:
        return set()
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    out = set()
    for m in _NUM.finditer(text):
        try:
            out.add(round(float(m.group(0).replace(",", "")), 6))
        except ValueError:
            continue
    return out


def ungrounded_numbers(text, *sources) -> set[float]:
    """The numbers in `text` that appear in none of `sources`."""
    have = set()
    for s in sources:
        have |= numbers(s)
    return numbers(text) - have


def grounded(text, *sources) -> bool:
    """True when every number in `text` appears in at least one of `sources`."""
    return not ungrounded_numbers(text, *sources)


def keeps_numbers(text, required) -> bool:
    """True when `text` still carries every number in `required` (the LLM line didn't drop the
    top item's facts — a CPA, a distance, a time)."""
    return numbers(required) <= numbers(text)
