"""Build a race's land chart in the Lab and print it as JSON — a look at what the Deploy carries.

The Lab's Deploy push builds this chart itself (vps/lab/app/orinassets.py) and the Pi hands it to
the Orin with each re-route request (#139), so nothing needs copying aboard. This prints the same
chart, for a look or a test. Run it inside the Lab container:

    docker exec -i <lab container> python - <race_id> [course_id] [--open-water] \\
        < pi/orin/reroute/prep_chart.py > chart.json
"""
import json
import sys

from app import boats, optimizer, orinassets, store

CHART_KIND = orinassets.CHART_KIND   # job.CHART_KIND must match


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
    if not optimizer.course_bbox(d, cid):
        print("the course has no mark positions", file=sys.stderr)
        return 1
    boat = boats.get_boat(orinassets.boat_id())   # the boat the Deploy is for: its draft
    chart, why = orinassets.build_chart(d, cid, boat, open_water=open_water)
    if why:
        print(f"{why} Not writing a chart. If this course box truly has no land, add --open-water.",
              file=sys.stderr)
        return 1
    json.dump(chart, sys.stdout, separators=(",", ":"))
    print(f"\nsource {chart['source']}, {sum(chart['layers'].values())} cells blocked",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
