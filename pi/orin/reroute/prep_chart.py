"""Build a race's land mask in the Lab and print it as JSON, for the Orin's re-route.

The Orin can't build the shoreline mask itself (that needs GDAL and the 200 MB GSHHG shoreline),
so the Lab builds it — the same mask the Lab's optimizer routes on — and it's copied aboard.
Run it inside the Lab container, where the Lab's code, races and shoreline data live:

    docker exec -i <lab container> python - <race_id> [course_id] [--open-water] \\
        < pi/orin/reroute/prep_chart.py > chart.json
    scp chart.json agent-c4@<orin>:c4-reroute/chart/<race_id>__<course_id>.json

The file name it should get is printed on stderr. Without it, the Orin's re-route avoids the
race's islands and zones only, and says so on the result.
"""
import json
import sys

from app import optimizer, store, labstate, boats
from app.geo import obstacles as geo


def main(argv):
    open_water = "--open-water" in argv          # a course box with truly no land in it
    argv = [a for a in argv if a != "--open-water"]
    if len(argv) < 2:
        print("usage: prep_chart.py <race_id> [course_id] [--open-water]", file=sys.stderr)
        return 2
    d = store.get_race(argv[1])
    if not d:
        print(f"unknown race {argv[1]}", file=sys.stderr)
        return 1
    courses = d.get("courses") or []
    cid = argv[2] if len(argv) > 2 else (courses[0].get("id") if courses else None)
    bbox = optimizer.course_bbox(d, cid)
    if not bbox:
        print("the course has no mark positions", file=sys.stderr)
        return 1
    # the Lab's own choices: its chart source and the active boat's draft
    src = (labstate.get("coastline_source") or geo.COASTLINE_SOURCE or "natural_earth").lower()
    depth = boats.active_safety_depth_m()
    # LAND ONLY. The race's zones, island disks and rounding-side barriers are rebuilt on the Orin
    # from what's left of the course: a barrier of an island already rounded must not block a
    # later leg.
    field = geo.build_for_course(d, cid, bbox, source=src, safety_depth=depth,
                                 zones_on=False, islands_on=False)
    # The builder never fails: if the shoreline couldn't load it returns an EMPTY mask, which the
    # boat would take as "land checked". Refuse that unless the box really is open water.
    land_cells = sum(field.layers.get(k, 0) for k in ("coastline", "shoal", "obstruction"))
    if not land_cells and not open_water:
        print(f"no land in the mask (source {field.source}): the shoreline didn't load. Not "
              "writing a chart. If this course box truly has no land, add --open-water.",
              file=sys.stderr)
        return 1
    out = geo.field_to_dict(field)
    out["open_water"] = bool(open_water and not land_cells)
    # what it was built from: the Orin checks this against the course aboard before using it
    out.update(race_id=d.get("race_id"), course_id=cid, kind="land",
               fingerprint=geo.course_fingerprint(d, cid, depth))
    json.dump(out, sys.stdout, separators=(",", ":"))
    safe = lambda s: "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(s))[:60]
    print(f"\nsave as: {safe(d.get('race_id'))}__{safe(cid)}.json  "
          f"(source {field.source}, {sum(field.layers.values())} cells blocked)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
