"""GPX export — unit test for the chartplotter file: valid XML, marks as wpt, the recommended
variant as a navigable rte (named points + ETAs), variants=all adds tracks, escaping holds.

Run (baked image): docker cp vps/lab/test_gpx.py <lab>:/srv/ && docker exec <lab> sh -c "cd /srv && python3 test_gpx.py"
Runs on the host too (stdlib only): python3 vps/lab/test_gpx.py
"""
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, "/srv")
sys.path.insert(0, "vps/lab")
from app import gpx  # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


NS = {"g": "http://www.topografix.com/GPX/1/1"}
BUNDLE = {
    "race_id": "bayview-mackinac-2026", "recommended": "left", "generated_at": 1782820800,
    "variants": [
        {"id": "left", "summary": "West of the <rhumb> & up the shore",
         "route": {"path": [{"lat": 43.02, "lon": -82.40, "t": 1782820800},
                            {"lat": 43.50, "lon": -82.55, "t": 1782838000},
                            {"lat": 44.10, "lon": -82.70, "t": 1782860000}]}},
        {"id": "middle", "summary": "Rhumb line",
         "route": {"path": [{"lat": 43.02, "lon": -82.40, "t": 1782820800},
                            {"lat": 44.10, "lon": -82.45, "t": 1782861000}]}},
        {"id": "empty", "route": {"path": []}},
    ],
}
MARKS = [{"name": "Start", "lat": 43.00, "lon": -82.42},
         {"name": "Cove Island", "lat": 45.32, "lon": -81.73},
         {"name": "NoFix"}]   # no lat/lon → skipped

print("recommended only:")
text = gpx.bundle_gpx(BUNDLE, marks=MARKS, variants="recommended")
root = ET.fromstring(text)          # parses = well-formed XML
check("valid XML, gpx root", root.tag.endswith("gpx"))
wpts = root.findall("g:wpt", NS)
check("2 marks as wpt (fixless mark skipped)", len(wpts) == 2
      and wpts[0].find("g:name", NS).text == "C4-Start")
rtes = root.findall("g:rte", NS)
check("one navigable rte (the recommended variant)", len(rtes) == 1
      and "left" in rtes[0].find("g:name", NS).text
      and "recommended" in rtes[0].find("g:name", NS).text)
rtepts = rtes[0].findall("g:rtept", NS)
check("route points named + timed", len(rtepts) == 3
      and rtepts[0].find("g:name", NS).text == "C4-left-01"
      and rtepts[0].find("g:time", NS).text.startswith("2026-"))
check("summary escaped (the <rhumb> survived)", "&lt;rhumb&gt;" in text)
trks = root.findall("g:trk", NS)
check("recommended also drawn as a track", len(trks) == 1)

print("variants=all:")
text = gpx.bundle_gpx(BUNDLE, marks=MARKS, variants="all")
root = ET.fromstring(text)
check("still exactly one navigable rte", len(root.findall("g:rte", NS)) == 1)
check("two tracks (left + middle; empty-path variant skipped)",
      len(root.findall("g:trk", NS)) == 2)

print("degenerate:")
text = gpx.bundle_gpx({"race_id": "x", "variants": []}, marks=None)
check("empty bundle still yields valid GPX", ET.fromstring(text).tag.endswith("gpx"))

print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
