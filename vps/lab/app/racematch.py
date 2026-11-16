"""Does a freshly read race look like one already saved? (re-reading the SIs, race week)

The AI names a draft's race_id from the name and year, so the same race read twice can come back as
"sorc-ocean-race-2026" one time and "2026-sorc-ocean-race" the next. Saved under a new id, the SIs
would land as a second race with no fleet, boat, rating pick or notes. `find` names the saved race
a draft most likely updates, so the review can offer to save over it.
"""
import datetime
import re

# words that don't tell two races apart
_STOP = {"the", "a", "an", "of", "and", "race", "races", "regatta", "annual", "edition", "yacht",
         "club", "sailing", "series", "nor", "si", "sis", "notice", "instructions"}


def _words(name):
    """The name's telling words. Ordinals and numbers stay: "1st Winter Series Race" and "2nd Winter
    Series Race" are different races ("78th Bayview Mackinac" still contains "Bayview Mackinac")."""
    s = re.sub(r"\b(?:19|20)\d\d\b", " ", str(name or "").lower())
    return {w for w in re.findall(r"[a-z0-9]+", s.replace("'", "")) if w not in _STOP}


def _date(v):
    try:
        return datetime.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _year(d):
    y = d.get("year")
    if isinstance(y, int) or (isinstance(y, str) and y.isdigit()):
        return int(y)
    dt = _date(d.get("start_date"))
    return dt.year if dt else None


def _score(draft, saved):
    """0 (not the same race) .. 1. Same year needed when both know it; start dates more than two days
    apart are different races (a weekly series is a week apart; a postponed start moves a day or
    two); then how much of the name the two share."""
    yd, ys = _year(draft), _year(saved)
    if yd and ys and yd != ys:
        return 0.0
    dd, ds = _date(draft.get("start_date")), _date(saved.get("start_date"))
    if dd and ds and abs((dd - ds).days) > 2:
        return 0.0
    a, b = _words(draft.get("name")), _words(saved.get("name"))
    if not a or not b:
        return 0.0
    na, nb = ({w for w in x if w[0].isdigit()} for x in (a, b))
    if na and nb and na != nb:                      # "Race 1" vs "Race 2", "1st" vs "2nd"
        return 0.0
    share = len(a & b) / len(a | b)
    if (a <= b or b <= a) and len(a & b) >= 2:      # "SORC Ocean" in "2026 SORC Ocean Race Offshore"
        share = max(share, 0.75)
    if share and dd and ds and dd == ds:            # breaks a tie between two editions of a series
        share += 0.01
    return share


def find(draft, saved_races, threshold=0.6):
    """The saved race this draft most likely updates: {race_id, name, same_id[, sure]}, or None.
    same_id: the draft already carries that race's id (saving updates it as it is)."""
    if not isinstance(draft, dict):
        return None
    rid = draft.get("race_id")
    saved = [s for s in saved_races or [] if isinstance(s, dict) and s.get("race_id")]
    for s in saved:
        if rid and s["race_id"] == rid:
            return {"race_id": rid, "name": s.get("name") or rid, "same_id": True}
    scored = sorted(((_score(draft, s), i) for i, s in enumerate(saved)), reverse=True)
    if not scored or scored[0][0] < threshold:
        return None
    if len(scored) > 1 and abs(scored[0][0] - scored[1][0]) < 1e-9:
        return None                                 # two saved races fit equally: don't guess
    best = saved[scored[0][1]]
    # sure: both start dates known and close. Without them a weekly race's editions look alike,
    # so the review offers the match unticked instead of saving over it by default.
    dd, ds = _date(draft.get("start_date")), _date(best.get("start_date"))
    return {"race_id": best["race_id"], "name": best.get("name") or best["race_id"], "same_id": False,
            "sure": bool(dd and ds)}
