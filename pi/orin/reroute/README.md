# Onboard re-route (Orin)

The Lab's full multi-model route optimizer, run on the boat's Orin from the boat's position to the
remaining marks. Issue #124. The crew's "Re-route now" button (#125) will call it through the Pi.

- **Runs only when asked.** One HTTP request starts one run. No timer, no triggers. One run at a
  time; a second request while one is running gets "busy" plus progress.
- **Same code as the Lab.** `vps/lab/app/routerun.py` is the Lab's own route run (wind field, boat
  overlays, optimizer). The Lab's Optimize button and this service both call it.
- **One download layer.** It reads the onboard plume's GRIB cache (`pi/orin/plume`). Offline it uses
  what's cached. Online it may download what's missing, within a time budget.
- **Legal in-race (RRS 41):** the boat's own computer, own position and polars, public forecasts.
  The result is labelled "Off the playbook", like the Pi's small re-route.

The Pi's small re-router (`vps/agent/app/reoptimize.py`) stays as the fallback when the Orin is down.

## How a run goes

```
iPad ──POST /api/reroute/run──▶ Pi engine ──POST /run──▶ Orin service ──spawns──▶ job.py
                                 (orinroute.py)            (service.py)            (one run)
iPad ──GET /api/reroute───────▶ Pi engine ──GET /status, /result──▶ Orin
```

1. The Pi builds the request: boat position, the deployed course (the RaceDefinition the Lab's
   Deploy push sent to `/course/load`) and which mark is next, the plan's track, who asked.
2. `job.py` cuts the course to what's left: it starts at the boat and runs from the next mark to the
   finish. Islands on legs already sailed are dropped.
3. Land: the Lab's own land mask for the course, carried aboard as a file (see "Chart" below),
   used only if it was built from the course aboard. Without one, the run avoids the race's
   islands and zones only, and the result says "No chart aboard. Route doesn't check the shore."
   (or "Chart is out of date. …" for a mask built from an older course).
4. Weather (`orin_weather.py`): the Lab's wind loader runs unchanged on wrapped model sources.
   Cached file first. Download only while online and inside the budget (20 s a request, 180 s in
   all). Offline it picks the cached cycle that covers the run best and doesn't step back cycles.
   Cache files are keyed by download box, so the run uses a box the cache already holds when one
   covers the course: the plume's box (the plan's route + 0.5°), or one an earlier re-route used.
5. `routerun.run_course` routes it (Lab defaults; no Opus briefing, no map grids, no venue model
   skill, current and sea state off unless enabled and online).
6. The result goes to `last_result.json`. The Pi fetches it once, stores it, and serves it to the
   console even with the Orin off.

## API (Orin, `http://10.10.10.2:8210`, from the Pi only)

| | |
|---|---|
| `POST /run` | body: the run request → `202 {ok, job_id}`; `409 {busy, job_id, progress, detail}` while running; `400 {detail}` |
| `GET /status` | `{state: idle\|running\|done\|error, job_id, asked_by, progress, last}` |
| `GET /result` | the last finished route, `404` if none yet |
| `GET /health` | `{ok: true}` |

`progress`: `{phase, step, note, elapsed_s, expected_s, pct}`. `step` is crew text ("Loading
forecasts", "Finding the fastest route"). `expected_s` is the last run's time.

Pi engine (`vps/agent/app/orinroute.py`, through the console's `/api/`):

| | |
|---|---|
| `POST /reroute/run` | body `{device?: "iPad 1"}` → `202` started, `409` busy (with progress) or nothing to route, `503 {detail: "Can't reach the Orin. Re-route not started."}` |
| `GET /reroute` | `{orin: {reachable}, state, progress, result, note?}`; `result` is the stored route with `age_s` |

Run request (Pi → Orin):

```json
{"device": "iPad 1", "asked_at": 1793552400,
 "position": {"lat": 44.4, "lon": -81.85, "t": 1793552400},
 "route": "bayview-mackinac-2026", "next_index": 1,
 "marks": [{"seq": 1, "name": "Start", "lat": 43.07, "lon": -82.42}, "..."],
 "definition": {"race_id": "...", "courses": ["the deployed course"], "zones": []},
 "course_id": "cove_island",
 "plan": {"variant": "left", "variant_label": "Left side", "points": [{"lat": 0, "lon": 0, "t": 0}]},
 "models": ["gfs", "nam", "hrrr"]}
```

`definition` is null when the course aboard was loaded before this build; the run then uses the
navigator's marks (no islands, no rounding sides) and says so. `models` is optional.

Result (`c4.reroute/v1`):

```json
{"schema": "c4.reroute/v1", "label": "Off the playbook", "off_playbook": true, "source": "orin",
 "job_id": "...", "asked_by": "iPad 1", "asked_at": 0, "computed_from": 0, "finished_at": 0,
 "run_s": 11.2, "peak_rss_mb": {"process": 55, "parser": 149},
 "from": {"lat": 44.4, "lon": -81.85}, "marks": ["Cove Island Virtual Gate", "Finish"],
 "available": true, "complete": true, "degraded": false,
 "finish_eta": 0, "total_hours": 33.3, "sailed_nm": 181.3, "tacks": 2, "first_heading": 351,
 "confidence": 0.74, "wind_coverage": 1.0, "timed_out": false,
 "points": [{"lat": 0, "lon": 0, "t": 0}], "leg_end_idx": [12, 35],
 "legs": [{"to": "...", "eta": 0, "leg_minutes": 0, "sailed_nm": 0, "tacks": 0, "sail": "J1",
           "point_of_sail": "beat", "first_heading": 0, "reached": true}],
 "sail_plan": ["S1", "J1"], "warnings": [],
 "vs_plan": {"available": true, "gain_min": -239, "plan_left_min": 1758, "new_left_min": 1998,
             "off_plan_nm": 1.4, "plan_finish_eta": 0, "variant": "left", "plan_label": "Left side"},
 "weather": {"models": [{"model": "gfs", "cycle": "2026-11-01 12Z", "cycle_epoch": 0,
                         "age_h": 5.0, "frames": 21, "expected_frames": 21}],
             "newest_age_h": 5.0, "oldest_age_h": 5.0, "online": false, "downloads": 0,
             "download_budget_spent": false, "box": "cache"},
 "chart": {"land": true, "source": "enc"}}
```

If the optimizer stalls short of the finish, `complete` is false, `finish_eta` and `total_hours`
are null, `vs_plan` is `{"available": false, "note": "Route incomplete."}` and the first warning is
"Route incomplete." A partial route never claims a finish time or a gain.

`vs_plan.gain_min` > 0 means the new route is faster. It is the plan's own time left from the
boat's nearest point on the plan, minus the new route's time left. The plan's times come from the
forecast it was built on, so this is a rough guide. Beyond 20 nm off the plan: "Too far off the
plan to compare."

## Chart (land), per race

The Orin has no GDAL and no shoreline data, so the Lab builds the land mask (the one its own
optimizer routes on: ENC or GSHHG, the active boat's draft) and it is copied aboard:

```bash
# on the VPS, from the repo checkout
docker exec -i sr33-dev-lab-1 python - bayview-mackinac-2026 cove_island \
  < pi/orin/reroute/prep_chart.py > /tmp/chart.json          # ~8 KB; stderr says the file name
scp /tmp/chart.json agent-c4@<orin>:c4-reroute/chart/bayview-mackinac-2026__cove_island.json
```

Needs the Lab build that has `obstacles.field_to_dict` (this PR). The file carries a fingerprint
of what it was built from: the course (marks, islands, rounding sides), the race's zones and the
no-go depth. The Orin checks it against the definition aboard. If the course was re-deployed since
(a new exclusion zone, a moved mark, another draft), the mask is not used: the run avoids the
current islands and zones only, and says "Chart is out of date. Route doesn't check the shore."
Rebuild and copy the chart after every course change.

## Measured on the Orin (2026-11-01)

Bayview Mackinac, Cove Island course, boat mid-race at 44.40N 81.85W (12 h into the plan), next mark
the Cove Island gate, then the finish. Same request, same GRIB files, same chart on both machines.

| | VPS | Orin |
|---|---|---|
| gfs + nam + hrrr, cached, warm | 6.4 s | 11–12 s |
| gfs + nam + hrrr, cached, first run (cold disk/imports) | — | 74 s |
| + ecmwf, cached | 9.7 s | 27–28 s |
| online, downloading all (gfs+nam+hrrr, 112 files) | 98 s | not run |
| peak memory, process + GRIB parser | 182 MB (291 MB with ecmwf) | 206 MB (330 MB with ecmwf) |
| route | 33.3 h, 181.3 nm, 2 tacks | identical (every point) |

Most of the Orin's time is the GRIB parser. The optimizer itself is ~6.5 s (VPS ~4.4 s).

## Install (owner, with his go — nothing here is installed by a script)

On the Orin, as `agent-c4`, from an up-to-date `~/Agent_C4` checkout. It uses the plume's venv
(`~/plume-venv`, same packages; see `pi/orin/plume/README.md`), so set that up first.

```bash
mkdir -p ~/c4-reroute/chart ~/c4-reroute/ingested/boats
# the Lab's reviewed boat profile (helm factor, approved polar overlay), if it differs from the repo's:
scp <vps>:<lab ingested>/boats/sr33.json ~/c4-reroute/ingested/boats/sr33.json
# the race's chart (see "Chart")

sudo cp ~/Agent_C4/pi/systemd/sr33-orin-reroute.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sr33-orin-reroute
journalctl -u sr33-orin-reroute -n 20    # "listening on 10.10.10.2:8210; runs only when asked"
# then, from the Pi:  curl -s http://10.10.10.2:8210/status   → {"state": "idle", ...}
```

The Pi engine needs this build (the `/reroute` endpoints, and `/course/load` keeping the
definition). Re-push the course from the Lab's Deploy tab once after the engine update, so the
definition is aboard.

Remove: `sudo systemctl disable --now sr33-orin-reroute`, delete the unit, `rm -rf ~/c4-reroute`.

## Settings (env, or `/etc/sr33/reroute.env`)

| var | default | |
|---|---|---|
| `REROUTE_MODELS` | `gfs,nam,hrrr` | the Lab's default blend. Add `ecmwf` (+16 s on the Orin) or `icon,gem` (online only) |
| `REROUTE_BIND` / `REROUTE_PORT` | `10.10.10.2` / `8210` | the cable address |
| `REROUTE_ALLOW_FROM` | `10.10.10.1,127.0.0.1` | who may call it |
| `GRIB_CACHE` | `~/c4-plume/gribcache` | the plume's cache |
| `REROUTE_DIR` | `~/c4-reroute` | state, jobs, last result |
| `REROUTE_CHART_DIR` | `$REROUTE_DIR/chart` | land masks, `<race_id>__<course_id>.json` |
| `REROUTE_BOAT` | `sr33` | boat profile id |
| `REROUTE_DOWNLOAD_BUDGET_S` / `REROUTE_REQUEST_TIMEOUT_S` | `180` / `20` | downloads, when online |
| `REROUTE_ROUTE_BUDGET_S` | `300` | optimizer time cap |
| `REROUTE_JOB_TIMEOUT_S` | `600` | a run is killed after this |
| `REROUTE_CURRENT` / `REROUTE_WAVES` | `0` / `0` | LMHOFS current / GLWU sea state (online only) |
| Pi: `ORIN_REROUTE_URL` | `http://10.10.10.2:8210` | |

## Tests

`pi/orin/reroute/test_reroute.py` (Lab image): the weather rules, the remaining course, one full
offline run from a fixture GRIB, the job lifecycle (busy, done, error, too long), the HTTP API.
`vps/agent/test_orinroute.py`: the Pi side, including refusal with the Orin unreachable.
`vps/lab/test_routerun.py`: the Lab's route is unchanged by the factoring. All run in `make ci`.
