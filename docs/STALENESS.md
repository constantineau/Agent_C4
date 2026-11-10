# Stale limits per channel and per consumer (#247)

**Status: decided by Cole, 2026-11-09.** The table lives in code as `shared/staleness.py`; every
consumer reads its limit from there. Built in #289: failover, the strip and `/conditions`, the
sail call, tactics, plan gap, the navigator, the collision watch, the bank tile, the per-group
stale alert (cloud alert and the iPad DATA tile), and the red age / "—" on the iPad. Built in
#291: the matcher, fatigue, wind trend, the navigator's ETA wind, the chartplotter feed, the DATA
tile's source rule, the cloud web page and the forecast tile's "Now" wind (rows marked **(#291)**).

Every number here is a **default**. It was measured on one boat's instruments; another boat's
sensors send at other rates. An install overrides any channel or consumer limit with
`STALENESS_OVERRIDES` (inline JSON or a file path; see `shared/staleness.py`).

## The rule (Cole, 2026-11-09)

- Each channel gets its own limit, and each consumer gets its own limit for each channel it reads.
- The numbers come from measured data, not guesses.
- When an input is too old, the **recommendation disappears** (no ETA, no CPA, no sail pick,
  no switch call).
- The **raw reading stays on screen with its age** (e.g. "Depth 6.2 m · 40 s old"). Past the dead
  limit the age is red. After **30 min** the number goes too and shows "—".

## Where the numbers come from

Measured 2026-11-06 from the Jul 18 race (6 h 40 m under way, full-res), the Aug 30 – Sep 2 dock
run (72 h), 33 days of uplink, and the live Pi. Full write-up: `/root/c4-ops/fixes-2026-11/t251-channels.md`
(private ops folder).

| What | Measured |
|---|---|
| Longest gap, 10 Hz channels (wind, heading, GPS, ROT) | 0.6 s (one Pi hiccup, same instant on every channel) |
| Longest gap, 4–5 Hz channels (heel, pitch, STW, derived wind) | 0.8 s |
| Longest gap, depth (1 Hz) | 3.8 s |
| Longest gap, water temp (0.5 Hz) / bank voltage (0.67 Hz) | 2.4 s / 1.9 s |
| Sensors that went quiet on their own in 79 h | **none** |
| Whole-system blackouts | 89 min before the Jul 18 start; archiver died 20:40Z; 4 uplink outages > 5 min in a month (longest 43 h) |
| Cloud lag (Pi → Postgres) | 3–18 s normally, ~35 s on a missed 15 s bucket; bucket p99.9 36 s |
| AIS targets, cloud copy | per-target median 30 s, p95 79 s |

So: the sensors are steady and the pipeline is what fails. Onboard limits are set at about
5–20× the worst gap seen, so they would have raised **zero** false alarms on Jul 18 or at the dock.
Cloud limits are set by the uplink (15 s buckets plus up to ~36 s lag), not by the sensors.

Not measurable from stored data: sensor-to-Pi latency (NMEA 2000 frames carry no time; Signal K
stamps on arrival). Not checkable until spring: wind (mast is down) and depth (reads junk on the hard).

## Two limits per channel

- **Late:** show the age next to the number. Nothing is wrong yet.
- **Dead:** stop using it for advice. The number stays, its age turns red, the advice that needs it disappears.
- **Gone:** after 30 min the number is removed ("—").

Plus **failover** where a channel has two sources: past this age the lead is passed over for the next rank.
Aboard that is 5 s for the fast channels and 10 s for the slow ones; the cloud keeps 45 s.

| Channel | Rate | Onboard late | Onboard failover | Onboard dead | Cloud late | Cloud dead |
|---|---|---|---|---|---|---|
| Wind (AWS, AWA, TWS, TWA, TWD) | 10 Hz | 2 s | 5 s | 15 s | 45 s | 120 s |
| Heading (true, magnetic), rate of turn | 10 Hz | 2 s | 5 s | 15 s | 45 s | 120 s |
| GPS position, COG, SOG | 10 Hz, 2 GPS | 2 s | 5 s | 10 s | 45 s | 120 s (60 s for collision watch) |
| Heel, pitch | 4–5 Hz | 2 s | 5 s | 15 s | 45 s | 120 s |
| Boat speed (STW) | 5 Hz, 1 source | 2 s | n/a | 15 s | 45 s | 120 s |
| Depth | 1 Hz, 1 source | 5 s | n/a | 15 s | 45 s | 120 s |
| Water temp | 0.5 Hz | 10 s | 10 s | 60 s | 60 s | 300 s |
| Bank voltage | 0.67 Hz | 10 s | n/a | 60 s | 60 s | 300 s |
| AIS targets | 2 s – 3 min per target | n/a | n/a | 5 min (drop target) | n/a | 5 min |
| Rudder angle | not published | – | – | – | – | – |

## Channel × consumer

"Before" is what the code did before #289. **Bold** = it differed from the decided limit.
**(#291)** after a consumer = built in #291, not #289. Limits are onboard / cloud, in seconds.

| Channel | Consumer | Limit (s) | Before | What disappears | What stays | Evidence |
|---|---|---|---|---|---|---|
| Own position, COG, SOG | Collision watch | 10 / 60 | **30 / 120** (`ais.py:66`) | CPA/TCPA geometry, "GPS fix stale" shown | target list, ranges as last known with age | GPS never gapped > 0.6 s; 30 s at 20 kn closing is ~300 m of CPA error; cloud worst ~36 s + lag ≈ 52 s |
| Signal K link | Collision watch | 10 / n/a | **30** (`ais.py:78`) | "AIS link down" replaces the picture | last targets with age | the link carries 100+ messages/s; 10 s is ~1000 missed |
| AIS targets | Collision watch | 300 / 300 (drop target); feed silent 15 min | same (`ais.py:51`, `:70`) | the target | other targets | Class B under way 30 s, slow 3 min; cloud p95 79 s |
| Position, COG, SOG | Navigator (next mark, ETA, laylines, mark advance) | 10 / 120 | **30 / 120** (`navigator.py:137`, #286) | ETA, laylines, mark advance | last fix with age, next mark name | same GPS evidence; mark advance on an old fix steers the plotter wrong |
| Heading | Navigator | 15 / 120 | **30 / 120** (#286) | layline "on this tack" call | heading with age | heading never gapped > 0.6 s |
| TWS, TWD | Navigator (laylines, rounding prep) | 15 / 120 | **30 / 120** (#286) | laylines, tack/gybe call | wind with age | wind never gapped > 0.8 s |
| TWS, TWD | Navigator ETA wind (20-min window) (#291) | newest ≤ 30 / 120 | **window only** (`navigator.py:458`) | ETA | distance to mark | a window of old wind reads as steady wind |
| TWS, TWD | Wind trend (#291) | 30 / 120 | **120 / 120** (`trend.py:36`) | trend arrows, "wind shifted right/left" | current wind with age | 120 s is fine for the cloud, slow onboard |
| TWD | Tactics (shift read, 12-min window) | newest ≤ 15 / 120, and ≥ 50% of the window filled | **none on the newest value; ≥ 6 samples in 12 min** (`tactics.py:51-55` reads `_latest()` with no age check) | shift call ("lifted/headed", favoured side) | wind with age | 6 samples of a 10 Hz channel is 0.6 s of data in 12 min |
| TWD, position | Selector (Hold / Switch / Off-script) | follows tactics: 15 / 120 | **inherits tactics: no limit** (`selector.py`) | the verdict; card says wind is too old for a call | last verdict greyed, with its age | a switch call from a dead masthead is the worst case for the playbook |
| STW, TWS, TWA | Matcher % of polar (feeds selector, strategy) (#291) | newest ≤ 15 / 120, and ≥ 50% of the 10-min window | **≥ 6 samples in 10 min** (`matcher.py:68-69`) | % of polar, play match | STW, TWS with age | 6 samples in 10 min lets a dead wind sensor keep a play armed |
| TWS, TWA | Sail call (sail dial, SAIL tile) | 15 / 120 | **5 min** (strip window, `onboard_conditions.py:129`, used by `engine_app.py:339`) | sail pick, crossover | TWS, TWA with age | wind never gapped > 0.8 s; a 4-min-old TWA can sit across a crossover |
| TWS, TWD | Plan gap (20-min window) | newest ≤ 15 / 120 | **window only** (`plangap.py:36`, reads `_latest()`) | plan-vs-actual gap | – | same as tactics |
| Heading, heel, STW | Fatigue (8 / 40-min windows) (#291) | newest ≤ 30 / 120 | **window only** (`fatigue.py:39-44`) | fatigue index, rotate call | – | windows thin out instead of stopping |
| Bank voltage | Battery alarm (bank tile) | late 10, dead 60 / late 60, dead 300 | **45 min**, then "unknown" with last volts and age (`power.py:287`) | warn/act status, hours-to-11.0 V | last volts with age | voltage arrives every 1.5 s, worst gap 1.9 s |
| Depth | Depth alarm | 15 / 120; an active shoal alarm holds while blind | **5 min** (strip window) | a new alarm can't be raised or cleared on old depth; "Depth lost" instead | depth with age | worst gap 3.8 s; a 4-min-old depth must not clear an alarm |
| Every channel | Stale-data alert | per sensor group (wind, heading, GPS, heel, boat speed, depth, bank): warn at dead, danger at 60 / warn 120, danger 300 (bank: twice its dead limit); keep 30 s debounce. Aboard it is the DATA tile; in the cloud an alert per group. The whole-boat "data stale" alert stays. A group's alert outlives its number ("—" at 30 min) for up to 24 h since its last reading | **freshest of all channels**, warn 60, danger 180 (`alerts.py:38`, `onboard_conditions.py:149`) | – (this is the alert) | – | today one live GPS hides a dead depth or wind |
| Every channel | iPad number tiles | age red at dead; "—" after 30 min (wind tile, bank tile, the "where each number comes from" table). Age shown at late: (#291) | **only the poll age shows** (7 s, `dashboard.js:96`); a reading older than 5 min vanishes from the strip | nothing on the number tiles (raw) | the number and its age | Cole's rule: raw stays with its age |
| Every channel | iPad data / sensor-health tile ("went silent", "one stale") | follows failover: 5 / 45 (10 for slow channels). A source's "one stale" limit is the failover limit of the fastest channel it sends (#291) | **45** (`dashboard.js:1025`, `source_policy.py:33`) | – | source list | 45 s onboard is ~60× the worst fast gap |
| Every channel | Cloud web page, all-channels sheet (#291) | ⚠ at late (45), "⚠ N min old" at dead (120) | **⚠ after 30 s** (`app.js:256`) | – | every reading | per channel from the agent's `state` |
| TWS, TWD | iPad FORECAST tile "Now" (#291) | the strip's dead limit (15 / 120) | **wind log's newest point, up to 60 s** (`observedAt(0)`) | the "Now" wind | the −60 / −120 min history | the log kept a stopped wind as "Now" with no age |
| Ranked channels | Source failover | 5 (fast), 10 (slow) / 45 | **45 / 45** (`source_policy.py:33`, `tools.py:66`) | the lead source is passed over | the next rank's reading | a dead masthead leaves TWD on a dead sensor for 45 s today |
| Position | Chartplotter feed (Garmin) (#291) | 5 / n/a | **10** (`gpsout.py:61`, `n2kout_app.py:55`) | 129283/129284 live leg | route list (129285) | n2kout polls at 1 Hz; 10 s at 8 kn is 40 m |
| Heading, COG | Sensor health: heading vs COG | pairs within 2 s | same (`sensor_health.py:142`) | – | – | both 10 Hz; 2 s pairs every sample |

## What the code did before #289 (read at dev 7cfc483)

| Limit | Value | Where |
|---|---|---|
| Source failover (wall clock since #274) | 45 s, onboard and cloud | `shared/source_policy.py:33`, `vps/agent/app/tools.py:66` |
| Onboard live cache | 12 s, then the archive | `datasource_onboard.py:59` |
| Latest read (onboard archive, cloud) | no age limit: returns the newest sample with its time | `datasource_onboard.py:560-590`, `datasource.py:118` |
| Conditions / strip window | readings up to 5 min | `onboard_conditions.py:129`, `tools.py:197` |
| Strip "stale" | freshest of all channels > 30 s | `onboard_conditions.py:150`, `tools.py:224` |
| Stale-data alert | freshest of all > 60 s warn, > 180 s danger, 30 s debounce | `alerts.py:38`, `:48`, `:134-135` |
| Collision watch own fix | 30 s onboard / 120 s cloud | `ais.py:66` |
| Collision watch link | 30 s | `ais.py:78` |
| AIS targets | dropped after 5 min; feed silent after 15 min | `ais.py:51`, `:70` |
| Navigator (position, COG, SOG, heading, TWS, TWD) | 30 s onboard / 120 s cloud (merged in #286) | `navigator.py:137`, `_fresh` at `:420` |
| Navigator ETA wind | 20-min window | `navigator.py:458` |
| Tactics, plan gap | read `_latest()` with no age check; 12 / 20-min windows | `tactics.py:18`, `:51`; `plangap.py:36`, `:158` |
| Wind trend | newest wind > 120 s → "No live wind" | `trend.py:36` |
| Matcher % of polar | 10-min window, ≥ 6 samples | `matcher.py:68-69` |
| Fatigue | 8 / 40-min windows | `fatigue.py:39-44` |
| Power (bank) | 45-min window; past it "unknown" with last volts and age (fixed in #45.1) | `power.py:50`, `:287` |
| Chartplotter fix | 10 s | `gpsout.py:61`, `pi/n2kout/n2kout_app.py:55` |
| iPad poll age | age shown after 7 s; held 30 s | `dashboard.js:95-96` |
| iPad data tile | source unseen > 45 s → "one stale" | `dashboard.js:1025` |
| iPad re-route card | age shown after 30 s | `reroute.js:22` |
| Cloud web conditions | ⚠ after 30 s | `vps/web/public/app.js:256` |
| Archiver status | not recording after 90 s without an update | `shared/archiver_status.py:21` |

Out of scope (not boat sensors): forecast 6 h (`weather.py:31`), buoys 90 min (`buoys.py:43`),
tracker 45 min (`tracker.py:42`).

## How it is built

- One table of per-channel limits, `shared/staleness.py` (onboard and cloud), read by every
  consumer. No consumer keeps its own number. Onboard or cloud is the `DATA_SOURCE` switch.
- The latest-value reads keep returning any age, always with the time. Each consumer checks the
  age against its limit.
- The strip (`/conditions`) nulls a value past its dead limit, so everything built on it (the sail
  call, the depth alarm, the copilot's "Now:" line) stops using it. The raw value, its age and
  `ok` / `late` / `dead` are in `raw`; `dead_groups` drives the per-group alert. A channel
  silent for over 30 min leaves the strip ("—"); its group's alert stays up to 24 h.
- Tactics also needs half of its 12-min window filled (30 s buckets) before it calls a shift.
- One test per built row feeds an old input and checks the advice disappears and the raw value
  stays (`vps/agent/test_stale_limits.py`, `pi/console/test_dashboard.js`).
- Checked on the water in the spring sensor-loss test (`docs/RACE_READY.md`).

## Decided (Cole, 2026-11-09)

1. **Onboard dead = 10 s (GPS) / 15 s (wind, heading, depth, boat speed).** This replaced the
   30 s the navigator and the collision watch had since #286. Cloud limits stay as in the table.
2. **Raw numbers show with a red age, then "—" after 30 min stale.**
3. **Stale alert per channel**, not one all-channels alert. Built per sensor group, so one dead
   masthead is one "Wind" alert, not five.
4. **Backup-sensor failover after 5 s aboard** (10 s for the slow channels); the cloud keeps 45 s.
   **Cloud collision watch at 60 s** (120 s before); everything else in the cloud stays at 120 s.

Not in the decisions, set from the table: the bank tile's dead limit is 60 s aboard / 300 s in
the cloud (it judged a reading up to 45 min old before), and the collision watch's link limit is
10 s (30 s before).
