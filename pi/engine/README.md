# Onboard engine (Phase 9.1)

The **in-race-legal** tier of the three-tier architecture (see `docs/RRS41_COMPLIANCE.md` and
`docs/ONBOARD_ENGINE_SCOPING.md`). A small FastAPI service that runs **on the boat** (Pi 4) and
serves the same nav/sail/plot/tactics/route endpoints the iPad uses against the cloud — but
computed **onboard from the boat's own data**, with **no LLM and no cloud round-trip**. The
boat's own computer crunching its own sensors is Expedition-class and not an "outside source"
under RRS 41, so all of these are legal while racing.

## How it works

`engine_app.py` reuses the cloud agent's deterministic modules unchanged
(`app.navigator` / `app.tactics` / `app.routing` / `app.fatigue` / `app.sails` / `app.weather`).
Those read through `datasource.active()`; with `DATA_SOURCE=onboard` that resolves to
**`OnboardSource`** (`vps/agent/app/datasource_onboard.py`), which reads:

- **telemetry history** — the Phase-2 full-resolution SQLite archive (`sk_archive` volume,
  written by `pi/archiver`), mounted **read-only**;
- **freshest live value** — an in-process Signal K WS cache (lower latency than the ~2-s archive
  flush; the engine still works if the WS is down — it falls back to the archive);
- **polars** — parsed from the committed `vps/db/seed/polars_sr33.sql` (the one canonical polar
  source; no DB);
- **course marks** — a small local SQLite store on the `engine_state` volume (the boat has no
  `waypoints` Postgres table); holds the generated practice course.

The instrument strip / multi-source view is built by `app.onboard_conditions` (the cloud builds
it off Postgres in `tools.py`). No psycopg, no Anthropic key, no `/auth`, no `/ws` chat, no
alerting/summarizer/polar-analysis (those are cloud / C4 Performance Lab), and **no race gate**
(everything here is legal in-race).

## Endpoints (port 8200)

`/health` · `/conditions` · `/conditions/full` · `/sources` · `/fatigue` · `/sail` · `/course` ·
`/navigator` · `POST /course/practice` · `/tactics` · `/forecast` · `/route` · `/ais` ·
`POST /fleet/load` · `/fleet` · `POST /playbook/load` · `/deviation` · `/drift` · `/selector` · `/reoptimize`

`POST /playbook/load` freezes the Lab-2 playbook bundle aboard; the two Lab-3 branch triggers read
from it: `GET /deviation` (route-deviation — boat vs the active variant's optimal track: XTE /
along-track / time-behind / VMC) and `GET /drift` (forecast-drift — the live common forecast vs the
plan's frozen forecast reference: veered/backed + speed change). `GET /selector` unifies those two
plus the on-water wind shift into ONE recommendation — HOLD the recommended variant / SWITCH to a
pre-authored variant / OFF-SCRIPT (no branch aboard for the favoured side), with confidence. When it
says off-script, `GET /reoptimize` is the fallback: a FRESH route from the live position through the
remaining marks on own polars + the common Open-Meteo forecast (reuses the onboard isochrone, no GRIB),
flagged OFF-PLAYBOOK, with its divergence from the frozen plan — the graceful-degradation ladder
(pre-authored branch → onboard re-route). All deterministic and power the iPad Strategy card.

Parity reference: `vps/agent/app/main.py` (the cloud serves the same paths, plus auth/chat/
alerts and the RRS-41 race gate).

## Run (bench or boat)

The engine is part of `compose.pi.yml`, so it comes up with the rest of the Pi stack:

```bash
# bench, with sample N2K data (archiver fills the archive the engine reads):
docker compose -f compose.pi.yml -f compose.pi.sample.yml up -d --build
curl -s localhost:8200/health
curl -s localhost:8200/conditions
curl -s -X POST localhost:8200/course/practice   # drop a W/L course from live pos+wind
curl -s localhost:8200/navigator
curl -s "localhost:8200/route?target=finish"

# on the Pi:  CAN_IFACE=can0 docker compose -f compose.pi.yml up -d
```

In race mode the iPad points at this service over boat-local Wi-Fi instead of the cloud
(channel separation — Phase 9.2's iPad-side half). CORS is open so a browser can hit the engine
directly without a proxy.
