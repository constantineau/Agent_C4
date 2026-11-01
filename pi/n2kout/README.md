# n2kout — route and live leg to the Garmin 943

The Pi's only N2K transmitter. Listens on `127.0.0.1:8210` (host network; the engine on the
same Pi is its only caller).

- **Always, from startup:** a bus device. Claims source address 35 at startup (defends it against a higher
  NAME, moves to 128-247 against a lower one), answers ISO requests for 60928 (address claim),
  126996 (product info: "SR33 C4 Navigator"), 126464 (PGN lists), 126993 (heartbeat), and NAKs
  any other request sent to it. Heartbeat every 60 s. (Behaviour change, 2026-11: before
  this, n2kout sent nothing until a route was asked for; now the claim and the heartbeat go
  out from startup, route or not.)
- **On the crew's "Show on GPS":** the engine posts the route. Every second n2kout reads the
  boat's fix from the engine (`GET :8200/gps/fix`), advances the active waypoint (inside
  0.05 nm, or past the turn's bisector) and sends live 129283 (signed XTE, + = right of track)
  and 129284 (waypoint, distance, bearings, ETA from the closing rate). The route list 129285
  goes out every 5 s.
- **Obstacles:** n2kout holds no land, islands or zones. Before every advance it asks the
  engine (`POST :8200/gps/leg-check`) whether boat → next waypoint is clear, with the same
  checks the send makes. It re-checks the current leg every 30 s. Not clear, or no answer in
  2 s: no advance, the live nav data pauses (retried every 5 s), and the iPad says "Steer-to
  paused. Press Re-route now."
- **Course marks** (flagged by the engine) advance on the 0.05 nm radius only, never on the
  bisector: crossing it isn't rounding the mark. Route-only points use both.
- **No fix newer than 10 s:** no 129283/129284 at all. The 129285 route keeps going (it is the
  plan, not a claim about where the boat is).
- **Ends** on Clear, after 6 h (`N2KOUT_ROUTE_TTL_S`), or when the engine reports another
  course (any `/course/load`, practice course or course switch). Each end sends 129283 with
  "navigation terminated" and an all-n/a 129284, three times.

`GET /health` and `GET /status` (also via the engine's `/gps/status`) carry a `bus` block:
`link` ("sending" / "no bus" / "errors" — the iPad shows it as "GPS link: …"), our `address`,
`frames_sent`, `send_errors`, `last_send_error`, `can` (the kernel's controller state, bus-off
and restart counts, `restart_ms`), `peers` (source addresses heard in the last 5 min) and
`requests_to_us` (ISO requests addressed to us: the plotter asking who we are).

## What we don't know

The route has **never** appeared on the 943 on the water, and we don't know why. Nothing here
has been tested on real hardware. The only check so far is the encodings decoding cleanly in
canboatjs on `vcan0`.

- Whether the GPSMAP 943 draws a route that a third-party device sends in 129285 at all. Many
  MFDs only use an external navigator's 129283/129284 to fill data fields (XTE, BTW, DTW,
  waypoint name), and draw nothing on the chart. Garmin doesn't document how it handles 129285
  from another maker. Treat the line on the chart as a bonus, and the data fields as the goal.
- Whether the 943 needs its navigation data source set to us before it uses our 129283/129284,
  and what it does when its own navigation is active at the same time.
- Whether it accepts manufacturer code 2046 (the code open-source devices use) and our
  product info, or only lists us.
- How it shows "navigation terminated", and how quickly it drops our leg after the stream stops.

## Dockside checklist

Before the first test, re-install the CAN unit on the Pi (it now sets `restart-ms 100`):
`sudo cp pi/systemd/sr33-can0.service /etc/systemd/system/ && sudo systemctl daemon-reload &&
sudo systemctl restart sr33-can0`, then `ip -details link show can0` shows `restart-ms 100`.

1. **Bus link.** `curl -s localhost:8210/health | jq .bus`. Want `link: "sending"`, `can.state:
   "ERROR-ACTIVE"`, `send_errors` not climbing, `peers` not empty. `ERROR-PASSIVE` with no
   peers means nobody acknowledges our frames: wiring, termination, or the bus isn't powered.
2. **Our frames on the wire.** `candump -tA can0,00000023:000000FF` (our source address 35).
   Within a minute: `18EEFF23` (address claim) and `1DF01123` (heartbeat). After "Show on GPS":
   `0DF90323` (129283) and `0DF90423` (129284) every second, `19F90523` (129285) every 5 s.
   If `address` in /health isn't 35, use that address instead.
3. **The 943 lists us.** Settings > Communications > NMEA 2000 Setup > Device List: an entry
   "SR33 C4 Navigator". `requests_to_us` in /health should be above 0 (the plotter asked us).
4. **Data source.** Settings > Communications > Preferred Sources (or Data Sources): see if
   navigation data, XTE or waypoint can be set to "SR33 C4 Navigator". Note what's offered.
5. **Data fields.** Add XTE, Waypoint (name), Distance to waypoint, Bearing to waypoint and ETA
   to a data page. Send a route with a fresh fix. Do they show "C4-0N" and change as the boat
   moves? Is the XTE sign right (boat right of the track shows "steer left")?
6. **Chart.** Does a route line or the active waypoint appear on the chart?
7. **Marks.** A mark the route rounds wider than 0.05 nm keeps the steer-to on it. Check
   that the route's mark waypoints sit at the marks.
8. **No fix / clear.** Unplug the GPS (or stop Signal K): within 10 s the data fields should
   blank. Tap Clear: note what the 943 shows after "navigation terminated".

**If it fails, photograph:** the 943's Device List (and the entry's details page), the Preferred
Sources page, the data page with the nav fields, the chart with the route sent, and any message
the 943 shows. Save `curl -s localhost:8210/status` and 60 s of `candump -L can0 > dump.log`
taken while the route is sent.
