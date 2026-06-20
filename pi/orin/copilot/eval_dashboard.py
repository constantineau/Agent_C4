"""Eval harness for the dashboard brief (Tier-1 quality gate).

Runs a fixed set of canned tile snapshots through `dashboard_brief.make()` against the live LLM
and scores each on objective checks: did we get a real (non-fallback) brief, is it grounded to
the given tiles only, and — the thing the 7B used to get wrong — did it read the wind TREND in the
right direction (building vs easing)? Use it to measure the prompt/constrained-output work and to
catch regressions after any model/prompt change.

Run on the Orin (it needs the local LLM):  python3 -m copilot.eval_dashboard
"""
import json
import sys

from . import dashboard_brief

_BUILD = ["build", "rising", "increas", "freshen", "getting up", "more breeze", "more wind", "up to"]
_EASE = ["eas", "drop", "decreas", "dying", "lighten", "fading", "less breeze", "softening", "backing off"]


def _has(text, words):
    t = (text or "").lower()
    return any(w in t for w in words)


# Each case: tiles + a list of (check_name, fn(result, keys, blob) -> bool)
CASES = [
    {
        "name": "building wind (9→12→18)",
        "tiles": [
            {"key": "wind", "name": "TWS Trend", "value": "Now 18 kts; -60 min 12 kts; -120 min 9 kts", "sub": "", "status": "watch"},
            {"key": "sail", "name": "Sail", "value": "A2", "sub": "in range", "status": "ok"},
            {"key": "vmg", "name": "VMG", "value": "5.1 kts", "sub": "92% of target", "status": "ok"},
        ],
        "trend": "build",
    },
    {
        "name": "easing wind (17→12→8)",
        "tiles": [
            {"key": "wind", "name": "TWS Trend", "value": "Now 8 kts; -60 min 12 kts; -120 min 17 kts", "sub": "", "status": "ok"},
            {"key": "sail", "name": "Sail", "value": "A2 → A3", "sub": "crossover near", "status": "watch"},
        ],
        "trend": "ease",
    },
    {
        "name": "escalated: peel + tired helm",
        "tiles": [
            {"key": "sail", "name": "Sail", "value": "J1 → A3", "sub": "peel before bear-away", "status": "act"},
            {"key": "charge", "name": "Crew Energy", "value": "26", "sub": "rotate soon", "status": "act"},
            {"key": "eta", "name": "Time to Mark", "value": "4 min", "sub": "Cove Island", "status": "watch"},
            {"key": "data", "name": "Data", "value": "5", "sub": "sources live", "status": "ok"},
        ],
        "must_note": {"sail", "charge"},   # the act tiles should be among the notes
    },
    {
        "name": "all calm",
        "tiles": [
            {"key": "vmg", "name": "VMG", "value": "5.4 kts", "sub": "96% of target", "status": "ok"},
            {"key": "sail", "name": "Sail", "value": "J1", "sub": "in range", "status": "ok"},
            {"key": "data", "name": "Data", "value": "5", "sub": "sources live", "status": "ok"},
        ],
    },
]


def run():
    total = 0
    passed = 0
    for c in CASES:
        keys = {t["key"] for t in c["tiles"]}
        r = dashboard_brief.make(c["tiles"])
        notes = r.get("notes", []) or []
        blob = " ".join([r.get("focus", "")] + [n.get("text", "") for n in notes])
        checks = []
        checks.append(("got LLM brief (not fallback)", r.get("mode") == "llm"))
        checks.append(("focus non-empty", bool(r.get("focus"))))
        checks.append(("notes grounded to given tiles", all(n.get("tile") in keys for n in notes) and len(notes) > 0))
        checks.append(("adjust grounded + valid", all(a.get("tile") in keys and a.get("status") in {"ok", "watch", "act"} for a in (r.get("adjust") or []))))
        if c.get("trend") == "build":
            checks.append(("reads wind as BUILDING (not easing)", _has(blob, _BUILD) and not _has(blob, _EASE)))
        elif c.get("trend") == "ease":
            checks.append(("reads wind as EASING (not building)", _has(blob, _EASE) and not _has(blob, _BUILD)))
        if c.get("must_note"):
            covered = {n.get("tile") for n in notes}
            checks.append(("flags the act tiles " + str(sorted(c["must_note"])), c["must_note"].issubset(covered)))

        print("\n■ " + c["name"] + "  [mode=" + str(r.get("mode")) + "]")
        print("  focus: " + (r.get("focus") or "(none)"))
        for n in notes:
            print("   - (" + str(n.get("tile")) + ") " + str(n.get("text", ""))[:90])
        for name, ok in checks:
            total += 1
            passed += 1 if ok else 0
            print(("  PASS " if ok else "  FAIL ") + name)
    print("\n==== SCORE: %d/%d checks passed ====" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
