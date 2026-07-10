"""Pure predicate semantics — the eval's ground-truth oracle.

This REIMPLEMENTS the three pure rules of the Tier-1 matcher (vps/agent/app/matcher.py:
`_pred_ok`, the applicability leg gate, and the sustain discipline) instead of importing them:
`app.matcher` drags the whole engine stack in (deviation/drift/tactics/datasource) and the Orin's
copilot venv can't hold that. test_eval.py LOCK-STEPS this copy against the real `_pred_ok`
wherever the engine code is importable (the dev VM), so a drift between the two fails a test
rather than silently mislabeling the corpus.

The sustain state machine is deliberately NOT replicated — the eval works on snapshots, not time,
so a scenario simply DECLARES whether each play's conditions have been held long enough
(`sustained[pid]`, default True). Conditions all true + not sustained = "arming", the same
observable the live matcher reports mid-sustain.
"""


def pred_ok(op, actual, value):
    """Byte-for-byte the semantics of matcher._pred_ok (kept in lock-step by test_eval)."""
    if actual is None:
        return False
    try:
        if op == ">=":
            return float(actual) >= float(value)
        if op == "<=":
            return float(actual) <= float(value)
        if op == "==":
            if isinstance(actual, list):
                return str(value).upper() in [str(a).upper() for a in actual]
            if isinstance(actual, bool) or isinstance(value, bool):
                return bool(actual) == bool(value)
            return str(actual).upper() == str(value).upper()
    except (TypeError, ValueError):
        return False
    return False


def _applicable(play, signals):
    """The leg gate: a hard-gated play only arms on its applicable leg(s); fails OPEN when the
    current leg is unknown. Mirrors matcher._evaluate (pace plays default to hard)."""
    applic = play.get("applicability") or {}
    legs = applic.get("legs")
    hard = (applic.get("gate") == "hard"
            or (applic.get("gate") is None and (play.get("scenario") or {}).get("kind") == "pace"))
    if hard and isinstance(legs, list) and legs:
        cur = signals.get("current_leg")
        if cur is not None:
            return cur in legs
    return True


def status(play, signals, sustained=True):
    """One play's verdict on a snapshot: 'armed' | 'arming' | 'quiet'."""
    preds = ((play.get("conditions") or {}).get("predicates")) or []
    all_ok = bool(preds) and _applicable(play, signals)
    for p in preds:
        all_ok = all_ok and pred_ok(p.get("op"), signals.get(p.get("signal")), p.get("value"))
    if not all_ok:
        return "quiet"
    return "armed" if sustained else "arming"


def status_map(library, signals, sustained=None):
    """{play_id: status} over a whole bundle — the eval's stand-in for the live matcher's map."""
    sustained = sustained or {}
    return {str(p["id"]): status(p, signals, sustained.get(str(p["id"]), True))
            for p in (library.get("plays") or [])}


def armed_set(library, signals, sustained=None):
    return {pid for pid, st in status_map(library, signals, sustained).items() if st == "armed"}
