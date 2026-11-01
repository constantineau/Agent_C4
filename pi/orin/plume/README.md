# Onboard forecast plume (Orin)

The Lab's forecast plume (`vps/lab/app/plume.py`) freezes at the gun: it is shore-computed and
route-specific, so under RRS 41 it is pre-start homework only. This service builds the SAME
plume on the boat's own Orin from public model data the Orin downloads itself, so the plume
keeps updating through a race. Own computer + public data is legal in-race.

It reuses the Lab's code as-is (`app.plume.record` / `build_plume`, `app.wind.models`), so the
lines are the same models, sampled the same way, in the same `c4.plume/v1` shape. It never runs
the route optimizer. Its GRIB cache (the Lab's layout under `GRIB_CACHE`) is the download layer
issue #124's onboard optimizer can reuse.

## What one pass does

Every 15 min (systemd timer, short-lived process):

1. `GET /playbook/track` on the Pi engine (over the cable, `http://10.10.10.1:8200`): the active
   playbook route with ETAs. Once the planned start has passed and the boat is within 20 nm of
   the route, the ETAs move in whole hours to the boat's real progress (up to 12 h behind or
   ahead). No route, or its ETAs are over: the boat's position (`GET /conditions`), snapped to
   a 0.25° grid.
   Each target gets its own run store, keyed by what the samples depend on: route geometry +
   ETAs + shift, or the snapped point. A moved boat or a replaced playbook never reuses old samples.
2. If the internet is up (a 5 s TCP probe to NOMADS), download any NEW cycle of
   gfs, nam, hrrr, ecmwf, icon, gem for the route's box. Offline: keep the last runs. If the
   target changed while offline (e.g. the boat fell another hour behind), resample the model
   files already cached, with no network (ICON/GEM need the network and drop out until back online).
   If nothing usable covers the new target (e.g. no playbook and the boat crossed a 0.25° grid
   line), it posts a small "waiting" marker instead, so the engine stops serving the old spot's
   onboard plume. The iPad then says "Onboard forecast: waiting for internet." (a shore plume, if
   aboard, is still chosen by the normal rules).
3. Build the plume, mark it `source: onboard`, `POST /plume/onboard` to the engine.
4. Delete cached GRIBs older than `GRIB_KEEP_H` (30 h), and run stores of targets untouched for 48 h.

A cycle that is due by the model's nominal lag but not posted yet (ECMWF is often hours late) is
re-probed at most once an hour. A newer due cycle is tried at once.

The console shows it as `Onboard · GFS 12z, 40 min old` and never as frozen. The engine shows
whichever of the shore and onboard plumes holds the newer weather, judged by model cycle: over the
models both carry, the one with the newer cycle on more models wins; a tie goes to the newer
freshest shared model; then the more recently recorded; onboard on a full tie.

## Cost (measured on the Orin, 2026-11-01, Mackinac box)

- Full refresh, all 6 models: ~16.6 MB, ~80 s. Nothing new: ~0 MB; ~20 s measured, mostly ECMWF re-probes, which
  the hourly back-off now skips (not re-measured on the Orin).
- ECMWF is ~88% of that (open data has no box subset: ~1.3 MB a frame, global).
- About 85 MB/day over Starlink if every new cycle is taken.
- Peak memory ~250 MB (process) + ~265 MB (GRIB parser child). The unit caps it at 900 MB.
- Ensembles (GEFS, ECMWF-ENS) are left out: ECMWF-ENS would be ~700 MB a refresh.

## Who may post an onboard plume

The engine takes `POST /plume/onboard` only when:

- the TCP peer is the Orin's end of the direct cable (`PLUME_ONBOARD_FROM`, default `10.10.10.2`),
- no forwarding header is present (`X-Forwarded-For`, `X-Real-IP`, `Forwarded`) — the console's
  nginx proxies `/api/` from `127.0.0.1` and passes client headers through, and uvicorn trusts
  `X-Forwarded-For` from `127.0.0.1`,
- the payload says `source: onboard`.

Shore reaches the Pi over Tailscale (`100.x`) and can't open a TCP connection from `10.10.10.2`.
The shore `POST /plume/load` rules are unchanged: still refused once a race is on.

## Install (owner, with his go — nothing here is installed by a script)

On the Orin, as `agent-c4`, from an up-to-date `~/Agent_C4` checkout:

```bash
python3 -m venv ~/plume-venv
~/plume-venv/bin/pip install -r ~/Agent_C4/pi/orin/plume/requirements.txt
~/plume-venv/bin/python -m eccodes selfcheck          # "Your system is ready."
mkdir -p ~/c4-plume

# one dry pass by hand (downloads + pushes; ~80 s the first time)
cd ~/Agent_C4/pi/orin/plume && ENGINE_URL=http://10.10.10.1:8200 \
  PYTHONPATH=~/Agent_C4/vps/lab ~/plume-venv/bin/python onboard_plume.py

sudo cp ~/Agent_C4/pi/systemd/sr33-orin-plume.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sr33-orin-plume.timer
journalctl -u sr33-orin-plume -n 30                   # one line per model + the push result
```

Separate venv on purpose: the copilot's `~/copilot-venv` stays untouched. The Pi engine needs
the matching engine build (the `/plume/onboard` endpoint, and ETAs on `/playbook/track`) before
the push is accepted.

Remove: `sudo systemctl disable --now sr33-orin-plume.timer`, delete the two unit files,
`rm -rf ~/plume-venv ~/c4-plume`.

## Settings (env, or `/etc/sr33/plume.env`)

| var | default | |
|---|---|---|
| `ENGINE_URL` | `http://10.10.10.1:8200` | Pi engine over the cable. Not Tailscale (refused). |
| `PLUME_MODELS` | `gfs,nam,hrrr,ecmwf,icon,gem` | drop `ecmwf` to cut ~88% of the download |
| `GRIB_CACHE` | `~/c4-plume/gribcache` | downloaded model files |
| `PLUME_DIR` | `~/c4-plume/store` | run history (48 h per model) |
| `GRIB_KEEP_H` | `30` | prune age for model files |
| `PLUME_MISS_BACKOFF_MIN` | `60` | wait before re-probing a due-but-missing cycle |
| `PLUME_ONLINE_PROBE` | `nomads.ncep.noaa.gov:443` | the "is the internet up" check |

## Tests

`pi/orin/plume/test_onboard_plume.py` builds a plume from a small fixture GRIB (runs in the Lab
image, which has eccodes); `vps/agent/test_plume_onboard.py` pins the engine's accept/refuse
rules. Both run in `make ci`.
