"""#325 — the Lab's web shell after a deploy, and the one-click chart override route.

  1) the static shell (/, index.html, app.js, styles.css, a vendor file) is served with
     `Cache-Control: no-cache`, so a browser asks each time (ETag: a 304 when nothing changed)
     instead of running an old app.js for hours after a deploy;
  2) /api/health (open, no login) names the web build; the hash changes when any shell file does;
  3) POST /api/races/{rid}/chart-override is one click: no name, no reason (by "Lab", why blank);
     `remove: true` takes it off; a Lab page from before #325 (empty who + why) still removes.
"""
import json
import os
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
_tmp = tempfile.mkdtemp()
os.environ["INGESTED_DIR"] = _tmp
os.environ["WEB_DIR"] = os.path.join(HERE, "web")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, main  # noqa: E402

ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not cond else ""))


client = TestClient(main.app)
TEAM = {"Authorization": "Bearer " + auth.issue_token()}

print("1) the static shell is no-cache, with an ETag")
for path in ("/", "/index.html", "/app.js", "/styles.css", "/vendor/leaflet.js"):
    r = client.get(path)
    check(f"{path}: 200, Cache-Control no-cache",
          r.status_code == 200 and r.headers.get("cache-control") == "no-cache",
          f"{r.status_code} {r.headers.get('cache-control')}")
r = client.get("/app.js")
etag = r.headers.get("etag")
r2 = client.get("/app.js", headers={"If-None-Match": etag})
check("unchanged app.js revalidates to a 304", etag and r2.status_code == 304, r2.status_code)
check("...and the 304 is no-cache too", r2.headers.get("cache-control") == "no-cache")
check("the index has the new-version bar", 'id="verbar"' in client.get("/").text)

print("2) /api/health names the web build")
h = client.get("/api/health")
check("open (no login), says the build", h.status_code == 200 and h.json().get("web") == main.WEB_VERSION
      and len(main.WEB_VERSION) == 12, h.text)
web = os.path.join(_tmp, "web")
shutil.copytree(os.path.join(HERE, "web"), web)
v1 = main.web_version(web)
check("same files, same build", v1 == main.web_version(web) == main.WEB_VERSION)
with open(os.path.join(web, "app.js"), "a") as fh:
    fh.write("\n// a deploy\n")
check("app.js changed: a new build", main.web_version(web) != v1)

print("3) the chart override is one click")
race = {"race_id": "t325", "name": "T", "courses": [{
    "id": "c", "start": {"lat": 44.0, "lon": -82.3},
    "marks": [{"name": "W", "type": "mark", "rounding": "port", "lat": 44.05, "lon": -82.3}],
    "finish": {"points": [{"lat": 44.0, "lon": -82.2999}]}}]}
with open(os.path.join(_tmp, "t325.json"), "w") as fh:
    json.dump(race, fh)


def ov():
    with open(os.path.join(_tmp, "t325.json")) as fh:
        return (json.load(fh)["courses"][0]).get("chart_overrides")


URL = "/api/races/t325/chart-override"
r = client.post(URL, json={"course_id": "c", "mark": "W"}, headers=TEAM)
got = ov() or {}
check("no name, no reason: saved, by Lab, why blank",
      r.status_code == 200 and (got.get("W") or {}).get("by") == "Lab" and got["W"]["why"] == ""
      and isinstance(got["W"].get("at"), int), f"{r.status_code} {r.text}")
check("the answer carries the course's overrides", "W" in (r.json().get("chart_overrides") or {}))
r = client.post(URL, json={"course_id": "c", "mark": "W", "remove": True}, headers=TEAM)
check("remove: one click, gone", r.status_code == 200 and not ov() and r.json()["chart_overrides"] == {},
      r.text)
client.post(URL, json={"course_id": "c", "mark": "W"}, headers=TEAM)
r = client.post(URL, json={"course_id": "c", "mark": "W", "by": "", "why": ""}, headers=TEAM)
check("a Lab page from before #325 (empty who + why) still removes", r.status_code == 200 and not ov())
r = client.post(URL, json={"course_id": "c", "mark": "Nope"}, headers=TEAM)
check("an unknown mark is still refused", r.status_code == 400 and not ov())
check("needs the team login", client.post(URL, json={"course_id": "c", "mark": "W"}).status_code == 401)

shutil.rmtree(_tmp, ignore_errors=True)
print("ALL OK" if ok else "SOME FAILED")
raise SystemExit(0 if ok else 1)
