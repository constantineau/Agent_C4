"""Rating feeds: where a race's handicap numbers come from, one feed per rating system (#311).

The race definition says which system it scores on (`rules_profile.scoring.rating_system`, see
shared/race_def.rating_spec). The fleet import asks this registry for that system's feed and never
reaches into a feed's internals, so PHRF or IRC can be added as a new feed without touching ORC.
Today only ORC is registered (in fleetimport). A system with no feed says so plainly.

A feed is any object with:
  system                                           — the RATING_SYSTEMS name it serves
  enrich(entries, definition, course_id, countries) → {ok, entries, matched, total, note?, ...}
  candidates(boat, sail, definition, course_id, countries, limit) → {boat, candidates, ...}

Also a small CLI to set a race's rating fields on a saved definition (deploy-time migration):
  python -m app.ratings set <race.json> --system ORC [--column C] [--column-tod C]
         [--course ID=TOT[/TOD] ...] [--countries USA,CAN] [--legacy-col C] [--dry-run]
It then clears every imported rating that is not off its boat's column now (the same rule as a
Lab save, shared/race_def.clear_stale_ratings) and prints what it cleared. `--legacy-col C` first
tags ratings imported before column tags existed with the column they are known to be off;
without it those are cleared, since their column is unknown.
"""
import argparse
import json
import sys

from shared import race_def

_FEEDS = {}


class RatingFeed:
    """Base for a rating feed. Subclasses set `system` and implement both calls."""
    system = ""

    def enrich(self, entries, definition, course_id, countries):
        raise NotImplementedError

    def candidates(self, boat, sail, definition, course_id, countries, limit=6):
        raise NotImplementedError


def register(feed):
    _FEEDS[feed.system.upper()] = feed
    return feed


def feed_for(system):
    return _FEEDS.get((system or "").upper())


def no_feed_note(system):
    """What the roster says for a system with no feed (plain, short)."""
    if system == "ONE_DESIGN":
        return "One-design: no ratings needed."
    return f"No {system} ratings feed yet. Enter ratings by hand."


# ---- deploy-time migration: put the rating fields into a saved race definition ----------------
def set_fields(definition, system=None, column=None, column_tod=None, courses=None, countries=None):
    """Return a copy of `definition` with the rating fields set (only the ones given)."""
    d = json.loads(json.dumps(definition))
    sc = d.setdefault("rules_profile", {}).setdefault("scoring", {})
    if system:
        sc["rating_system"] = system.upper()
    if column is not None:
        sc["rating_column"] = column
    if column_tod is not None:
        sc["rating_column_tod"] = column_tod
    if courses:
        rows = []
        for spec in courses:
            cid, _, cols = spec.partition("=")
            tot, _, tod = cols.partition("/")
            if not cid.strip() or not tot.strip():
                raise ValueError(f"bad --course {spec!r} (want ID=TOT or ID=TOT/TOD)")
            rows.append({"course": cid.strip(), "column": tot.strip(), "column_tod": tod.strip()})
        sc["course_columns"] = rows
    if countries is not None:
        sc["rating_countries"] = [c.strip().upper() for c in countries.split(",") if c.strip()]
    return d


def migrate(definition, system=None, column=None, column_tod=None, courses=None, countries=None,
            legacy_col=None):
    """set_fields, then tag legacy imports (if told their column), then clear stale numbers.
    Returns (new definition, {"tagged": n, "boats": [...], "own": bool})."""
    d = set_fields(definition, system, column, column_tod, courses, countries)
    tagged = race_def.tag_legacy(d, legacy_col) if legacy_col else 0
    hit = race_def.clear_stale_ratings(d)
    return d, dict(hit, tagged=tagged)


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m app.ratings")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set", help="set the rating fields on a saved race definition")
    s.add_argument("path")
    s.add_argument("--system")
    s.add_argument("--column")
    s.add_argument("--column-tod")
    s.add_argument("--course", action="append", default=[], help="ID=TOT[/TOD], repeatable")
    s.add_argument("--countries")
    s.add_argument("--legacy-col", help="column untagged ORC imports were read off")
    s.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    with open(a.path) as f:
        before = json.load(f)
    after, rep = migrate(before, a.system, a.column, a.column_tod, a.course, a.countries,
                         a.legacy_col)
    print(json.dumps(after["rules_profile"]["scoring"], indent=2))
    rated = sum(1 for e in after.get("fleet") or [] if e.get("rating") is not None)
    print(f"legacy imports tagged: {rep['tagged']}")
    print(f"ratings cleared: {len(rep['boats'])}"
          + (f" ({', '.join(rep['boats'][:10])}{', ...' if len(rep['boats']) > 10 else ''})"
             if rep["boats"] else ""))
    print(f"our boat's rating cleared: {'yes' if rep['own'] else 'no'}")
    print(f"boats still rated: {rated} of {len(after.get('fleet') or [])}")
    if a.dry_run or after == before:
        print("no change written" if after == before else "dry run: not written")
        return 0
    from . import atomicio
    atomicio.write_json(a.path, after, indent=2)
    print(f"written: {a.path}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
