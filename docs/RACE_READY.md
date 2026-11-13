# Spring race-ready checklist (#250)

**Status: decided by Cole, 2026-11-09.** This checklist is public. The filled-in results are not:
they go in the private ops folder (below).

## The rule (Cole, 2026-11-09)

C4 is race-ready when every item below has a recorded result and Cole has signed it off. Unit
tests and the blind matcher score don't count. Each item is a test on the real boat or the real
backups.

## How results are recorded

- Results live in the private ops folder on the VPS, `/root/c4-ops/race-ready-2027/`, never in
  this repo: it is public, and results carry boat details, addresses and hostnames.
- Each run fills one row of the results table in `results.md` there: date, PASS / FAIL, one line
  of what was seen, and where the evidence is. Cole's sign-off goes at the bottom of that file.
- Evidence (logs, `candump` files, photos, screenshots) goes in `<item>/` next to it.
- A FAIL gets an issue and the item is run again after the fix. The row keeps both results.
- Owner: **Cole** = needs hands on the boat or his decision. **Agent** = can be run from shore.
  Most boat items are both: Cole does the physical step, an agent watches and records.

## Order

Setup first (S1–S4), then the checks on the hard / at the dock (1–9), then the practice sail (10).
Re-run anything a later deploy touches.

## Spring setup

### S1. Swap the SD card (#181)

The card (unbranded "ASTC") has corrupted the archive three times (Jul 18, Oct 28, Nov 3).

- **Do:** fit a high-endurance card or a USB SSD. Bring the Pi up per `pi/SETUP.md` (fresh clone,
  one `compose up`). Then bring it to the deployed state per `pi/deploy/README.md`, and check
  `cat ~/DEPLOYED` on the box.
- **Pass:** all Pi services up (`docker ps`); `curl -s localhost:8200/health` ok; the archiver's
  background `quick_check` (runs after startup, then every 5 days, only with no session open)
  finishes clean at least once on the new card, seen in the archiver log; the old card is kept as a spare.
- **Owner:** Cole (hands), agent (prep and verify).

### S2. Copy-off aboard and the gated prune (#181, #243)

Follow "Spring deploy steps (#181)" in `pi/archiver/README.md`:

1. SSH key Pi → VPS, forced command (rsync into the receive dir + `copyoff.py verify`).
2. Pi timer: `copyoff.py seal` then `ship`, hourly when the link is up.
3. Run it a day or two and **measure** bytes sent (rsync `--stats` or the Starlink app). The
   estimate is 0.7–1 GB/day; it has never been measured.
4. The one-time backlog: only the live `archive.db` now. The damaged archives were deleted on
   Cole's go on 2026-11-03, so the README's 24 GB file is gone.
5. Rebuild the archiver with `ARCHIVE_OFFBOAT_LEDGER=/var/lib/sr33/archive/outbox/ledger.json`.
6. VPS: compress what arrives and copy it to the archive bucket.

- **Pass:** `copyoff.py status --outbox D/outbox` shows sealed files verified; the archiver's
  prune deletes only rows in the ledger (its log says so); measured GB/day written down here.
- **Owner:** agent (setup, measure), Cole (go for the archiver rebuild, which restarts recording).

### S3. Boat on the current code

- **Do:** deploy what is merged but not yet aboard (Pi per `pi/deploy/README.md`, Orin per
  `docs/REMOTE_OPS.md`). Backup and rollback images first, as for every deploy.
- **Pass:** `pi/deploy/record-deployed.sh` exits 0 for each deploy; `cat ~/DEPLOYED` on the Pi and
  the Orin matches the commit.
- **Owner:** agent, with Cole's go for each deploy.

### S4. Lab Deploy push for the first race

The push works any time (proven 2026-11-05 with a test race). It is listed here so the real race's
playbook is aboard and checked.

- **Do:** in the Lab, build and freeze the race's playbook, then Deploy tab → Push (course, chart,
  boat profile, playbook). A past race is refused, so push before the start.
- **Pass:** all 7 push steps green; Orin `signature_ok`; the iPad no longer says "Playbook not
  confirmed"; Re-route now works from the iPad.
- **Owner:** Cole (which race), agent (push and check).

## Checks

### 1. Cold boot without UTC

The Pi has no clock battery. After power-up its clock is wrong until NTP or a GPS fix sets it
(`shared/clock_trust.py`, #240).

- **Do:** unplug Starlink (no NTP) and disconnect the GPS on the N2K bus (or cover both antennas).
  Power the boat off and on. Wait 10 min. Reconnect the GPS. Wait 10 min. Starlink back on.
- **Watch:** `docker compose -f compose.pi.yml logs uplink archiver | grep -iE "clock|prune|undated"`.
- **Pass:**
  - recording runs during the untrusted stretch (archive rows exist for it);
  - the uplink logs "clock not trusted yet, held N readings undated" and posts nothing undated;
  - the archiver logs "prune waiting" and prunes nothing until trust;
  - within a minute of the GPS fix, trust latches; cloud rows after that carry the right time;
  - the iPad shows no time-based advice (ETA, start timer) from the wrong clock.
- **Owner:** Cole (power and plugs), agent (logs).

### 2. Sensor loss

What the crew sees when one sensor dies. Limits per `docs/STALENESS.md` (#247, built in #289;
the rest in #291). For a row not built yet, record what the boat does against that table.

- **Do:** at the dock, mast up, everything on. Unplug one device at a time for 2 min, then plug it
  back: masthead wind (GND10), GPS24xd, depth (Intelliducer), speed (GST10), Orca.
- **Watch:** iPad tiles and the data tile; `curl -s localhost:8200/health/sensors | jq`;
  `curl -s localhost:8200/conditions | jq`.
- **Pass, for each device:**
  - a two-source channel fails over to the next rank within the failover limit;
  - a one-source channel (depth, STW) keeps its last number with its age, and the age turns red at
    the dead limit;
  - the advice that needs it disappears (e.g. no sail pick without wind, no CPA without GPS);
  - sensor health says the device went silent; the stale alert fires for that channel;
  - everything recovers within 10 s of plugging back in.
- **Owner:** Cole (unplugging), agent (recording).

### 3. iPad wake

The iPad sleeps between legs. #120 changed "Engine unreachable" to "Reconnecting" on wake.

- **Do:** console open, let the iPad lock for 15 min, then wake it. Repeat with Wi-Fi toggled off
  and on while locked.
- **Pass:** never "Engine unreachable" on a normal wake; "Reconnecting" for at most 20 s, then
  live numbers; tiles held from before the sleep show their age, not as current.
- **Owner:** Cole.

### 4. Repeat-course race

Two races on the same course in one day (#41, #246, merged in #286).

- **Do:** load a practice course, tap Start (twice), sail or replay it to the finish. Tap Race over
  if #241 has landed by then. Tap Start again and sail it a second time. Mid-course, try "Undo last mark" and the next-mark
  picker once each. Can be rehearsed ashore first with the replay rig (`tools/replay/`).
- **Pass:** the second run starts at mark 1, not at the finish; a mark advances only after two
  distinct fixes; Undo steps back one mark change; the picker sets the mark; the Garmin's steer-to
  follows the iPad's next mark.
- **Owner:** Cole (on the water), agent (replay rehearsal).

### 5. Long outage (uplink backlog replay)

A long Starlink outage must lose nothing (#243, #283: no queue cap, paged replay).

- **Do:** with everything on, unplug Starlink for 2 h (longer if the dock allows). Plug it back.
- **Watch:** queue size on the Pi (`docker compose -f compose.pi.yml exec uplink sh -c 'ls /var/lib/sr33/queue/*.json 2>/dev/null | wc -l'`);
  uplink log; on the VPS, rows per minute in `telemetry_raw` over the outage window.
- **Pass:** the queue drains to zero; the cloud has 15 s rows across the whole outage, no gap;
  live data keeps its 15 s cadence while the backlog replays; nothing in `quarantine/`.
- **Owner:** Cole (unplug), agent (checks).

### 6. Orin under load (#280)

The Orin has 7.5 GB and no swap. On 2026-11-09 the LLM's prompt cache grew until the box froze
~20 min and the kernel killed it. #281 capped the cache at 512 MiB.

- **Do:** for at least 3 h (one long race), run copilot coaching, plume and two or three re-routes
  at the same time. A practice sail is the real test; a dock run with replayed data comes first.
- **Watch:** `tegrastats --interval 5000` logged to a file; `free -m` every minute;
  `journalctl -k | grep -i oom`; `curl -s localhost:8300/health`.
- **Pass:** no OOM kill and no freeze; available memory never below 900 MB; copilot health ok the
  whole time; each re-route finishes inside the 90 s limit; SSH answers throughout.
- **Owner:** agent, Cole (on board for the sail).

### 7. Route legality (#242)

The Lab must refuse a route it can't prove safe and complete. **Depends on PR #287 (open).**

- **Do:** build the spring race playbooks in the Lab. Then try one known-bad case: remove the
  Bayview Finish chart override and try to freeze.
- **Pass:** the bad case is refused at freeze, Deploy and GPX with "Route checks failed"; the real
  playbooks pass every check; every chart override is listed with who, when and why, and Cole has
  read each one.
- **Owner:** Cole (overrides), agent (builds and checks).

### 8. Backup restore drill

- **Do:** on the VPS, per `tools/backup/README.md`:
  `sudo C4_BACKUP_DEST=gs://constantineau-c4-nightly/vps-nightly python3 tools/backup/lab_restore.py`.
  For the Coldline archive bucket: `BUCKET=... SRC=... STAGE=... tools/backup/verify_restore.sh`.
- **Pass:** every artifact fetched, every SQLite `integrity_check` ok, row counts match, the dump
  restores into a fresh database (the 2026-11-03 drill was 56/56); `verify_restore.sh` hashes
  match the manifest. Also: one spring copy-off file (S2) restored and checked.
- **Owner:** agent.

### 9. Dockside Garmin check

The route has never appeared on the 943. Follow "Dockside checklist" in `pi/n2kout/README.md`.

- **Do:** check `ip -details link show can0` shows `restart-ms 100`. Then steps 1–8: bus link
  (`curl -s localhost:8210/health | jq .bus`), our frames (`candump -tA can0,00000023:000000FF`),
  the 943's Device List shows "SR33 C4 Navigator", data sources, data fields (XTE, waypoint,
  distance, bearing, ETA), chart line, wide mark rounding, GPS unplugged and Clear.
- **Pass:** `link: "sending"`, `ERROR-ACTIVE`, `requests_to_us` > 0; the 943 lists us; XTE and
  waypoint fields fill and the XTE sign is right; fields blank within 10 s of losing GPS. A route
  line on the chart is a bonus, not a pass condition.
- **If it fails:** the photos and dumps the README lists.
- **Owner:** Cole (943 menus), agent (Pi side).

### 10. Practice sail

The only integrated test with real wind. Run it as a race.

- **Do:** a practice course pushed from the Lab; Start; sail every leg; finish (Race over, if #241 has landed). During the sail,
  repeat the short versions of checks 2 (one sensor), 3, 6 and 9 on the water.
- **Pass:**
  - every iPad tile live and plausible against the instruments (wind, heading, depth, speed, bank);
  - marks advance at the real roundings; the Garmin steer-to follows;
  - the collision watch shows real AIS targets with sensible CPA;
  - the selector card settles (no flip-flopping between sides);
  - a re-route runs from the iPad and finishes inside 90 s;
  - the recording shows up in the Lab and the debrief runs on it;
  - nothing restarted that shouldn't (`docker ps` uptimes, Orin `journalctl`).
- **Owner:** Cole (sail), agent (debrief, logs).

## Results and sign-off

In `/root/c4-ops/race-ready-2027/results.md` (private). Race-ready means every item there is PASS,
or a FAIL Cole accepts in writing with the reason, and Cole has signed it.
