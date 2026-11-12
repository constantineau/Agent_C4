"""Saving a RaceDefinition: an explicit clear persists; a re-ingest draft keeps the stored prep fields.

_write_race restores the _PRESERVE_ON_SAVE fields (fleet, notes, watch plan…) from the stored copy
only when the save omits the key, or when a re-ingest draft sends them empty. An ordinary save that
sends an empty value — the user cleared the notes or emptied the roster — must be stored as sent
(issue #74: the Lab said "Saved" but the old notes came back on reopen).
Run in-container: python test_race_preserve.py
"""
import json
import os
import sys
import tempfile

os.environ["INGESTED_DIR"] = tempfile.mkdtemp()

from app import main  # noqa: E402

PRIOR = {"race_id": "t-race", "name": "T", "fleet": [{"boat": "Il Mostro"}], "own": {"boat": "C4"},
         "learnings_notes": "old notes", "tracker": {"provider": "yb"},
         "division_starts": {"A": 1}, "watch_plan": {"teams": {"A": {"members": ["x"]}}, "blocks": [1]}}


def _with_prior(fn):
    store = {"t-race": dict(PRIOR)}
    orig = main.store.get_race
    main.store.get_race = lambda rid: store.get(rid)
    try:
        return fn()
    finally:
        main.store.get_race = orig


def test_explicit_clear_persists():
    d = dict(PRIOR, learnings_notes="", fleet=[], own={}, division_starts={}, tracker={},
             watch_plan={"teams": {}, "blocks": []})
    _with_prior(lambda: main._write_race(d))
    with open(os.path.join(main.INGESTED_DIR, "t-race.json")) as f:
        disk = json.load(f)   # what's actually stored, not just the mutated input
    for k in ("learnings_notes", "fleet", "own", "division_starts", "tracker"):
        assert not disk[k], (k, disk[k])
    assert disk["watch_plan"]["blocks"] == []


def test_malformed_watch_plan_draft_does_not_crash():
    d = {"race_id": "t-race", "watch_plan": {"teams": ["A"]}}
    _with_prior(lambda: main._write_race(d, draft=True))
    assert d["watch_plan"] == {"teams": ["A"]}


def test_omitted_key_is_kept():
    d = {"race_id": "t-race", "name": "T"}
    _with_prior(lambda: main._write_race(d))
    for k in main._PRESERVE_ON_SAVE:
        assert d[k] == PRIOR[k], k


def test_reingest_draft_keeps_stored_prep_fields():
    # what the extractor + review form send: fleet [], no notes, an empty two-team watch skeleton
    d = {"race_id": "t-race", "name": "T (SIs)", "fleet": [], "own": {}, "learnings_notes": None,
         "watch_plan": {"teams": {"A": {"name": "A", "members": []}, "B": {"name": "B", "members": []}},
                        "blocks": []}}
    _with_prior(lambda: main._write_race(d, draft=True))
    for k in main._PRESERVE_ON_SAVE:
        assert d[k] == PRIOR[k], k
    assert d["name"] == "T (SIs)"


def test_draft_with_real_values_wins():
    d = {"race_id": "t-race", "fleet": [{"boat": "New"}], "division_starts": {"B": 2}}
    _with_prior(lambda: main._write_race(d, draft=True))
    assert d["fleet"] == [{"boat": "New"}] and d["division_starts"] == {"B": 2}
    assert d["learnings_notes"] == "old notes"


def test_reingest_draft_keeps_confirmed_rating_setup():
    # Codex on #333: a re-read of the NOR/SI that names no column must not wipe the one the user
    # picked, then clear every imported rating against the blank setup
    prior = {"race_id": "t-race", "name": "T", "venue_country": "GBR",
             "rules_profile": {"scoring": {"rating_system": "ORC", "rating_column": "TMF_Inshore"}},
             "fleet": [{"boat": "X", "rating": 1.2, "rating_col": "TMF_Inshore", "source": "yb+orc"}]}
    d = {"race_id": "t-race", "name": "T (SIs)", "fleet": [], "rules_profile": {"scoring": {}}}
    orig = main.store.get_race
    main.store.get_race = lambda rid: json.loads(json.dumps(prior))
    try:
        main._write_race(d, draft=True)
    finally:
        main.store.get_race = orig
    sc = d["rules_profile"]["scoring"]
    assert sc["rating_column"] == "TMF_Inshore" and sc["rating_system"] == "ORC", sc
    assert d["fleet"][0]["rating"] == 1.2, d["fleet"]
    assert d["venue_country"] == "GBR"
    # what the new extraction does name wins
    d2 = {"race_id": "t-race", "fleet": [], "rules_profile": {"scoring": {"rating_column": "TMF_Offshore"}}}
    main.store.get_race = lambda rid: json.loads(json.dumps(prior))
    try:
        main._write_race(d2, draft=True)
    finally:
        main.store.get_race = orig
    assert d2["rules_profile"]["scoring"]["rating_column"] == "TMF_Offshore"
    # a new race-wide column replaces an old per-course one; it doesn't sit under it
    prior3 = dict(prior, rules_profile={"scoring": {"rating_system": "ORC",
                  "course_columns": [{"course": "long", "column": "OLD"}]}})
    d3 = {"race_id": "t-race", "fleet": [], "rules_profile": {"scoring": {"rating_column": "NEW",
          "course_columns": []}}}
    main.store.get_race = lambda rid: json.loads(json.dumps(prior3))
    try:
        main._write_race(d3, draft=True)
    finally:
        main.store.get_race = orig
    sc3 = d3["rules_profile"]["scoring"]
    assert sc3["rating_column"] == "NEW" and not sc3.get("course_columns"), sc3


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print("ok", name)
    print(f"{n} passed")
    sys.exit(0)
