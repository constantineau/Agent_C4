# pi/deploy — putting the data path on the boat

The Pi runs its own clone of the repo on `main`. A `git pull` there would change what every
service runs, so a Pi deploy copies single files after diffing them, and rebuilds only the
services those files belong to (CLAUDE.md, Deploys). `deploy-data-path.sh` does exactly that for
the full-res archiver (plus the `shared/` modules it imports, and the uplink only if its own
files changed).

**Deploying it restarts full-res recording on the boat.** That is Cole's decision. Nobody
passes `--apply` without his go.

## What a deploy does

1. **Inventory (read-only).** The Pi's branch and commit, local changes, containers, archiver
   restart count, disk and RAM, the archive volume listing, the archiver's last log lines.
   Then the open race sessions in `engine.db` (read-only query) and the retention the running
   archiver uses now vs what it will use after the deploy. **`--apply` refuses if a session has
   been open for more than 24 h**, or if the sessions table can't be read, unless
   `--allow-open-session` is passed. An open session keeps everything since its start, so the
   prune frees nothing.
2. **Diff.** Every data-path file (`pi/archiver`, `pi/uplink`, `pi/signalk`, `shared`,
   `compose.pi.yml`; tests excluded) is checksummed on the Pi and here. Changed files are shown
   as a unified diff, new files by name. If `compose.pi.yml` changes anything outside the
   archiver/uplink blocks, or `pi/signalk` changes, it stops: it doesn't restart those
   services, so the change would sit there unapplied. It stops if the Pi has local edits to any
   of them,
   and it stops if any read on the Pi fails: a failed listing must never look like "every file
   is new", because rollback deletes the files it thinks were new.
3. **Backup** to `~/deploy-backups/data-path-<stamp>/` on the Pi: a tar of the files about to
   be overwritten, the list of new files, and the current images tagged `:pre-<stamp>`.
4. **Copy** the changed and new files into `~/Agent_C4`.
5. **Rebuild and restart only** the archiver (and the uplink if its files changed):
   `docker compose -f compose.pi.yml build archiver && … up -d --no-deps archiver`.
   Signal K, the engine, the console and n2kout are not touched.
6. **Verify.** Waits (up to 30 min; checking a 24 GB file on the SD card is slow) for the
   archiver to open its archive and write. Then it lists the volume, checks `archive.db` grows
   over 60 s, prints `archiver-status.json` and the retention now in effect, and checks the
   uplink is still sending.
   With `--expect-rotation` (the first deploy after the 2026-09-02 corruption) it instead
   requires the log to show the old file moved aside before the first write. If the archiver
   opened the old file without rotating it, the script stops the archiver and exits.
   `archive.corrupt-<stamp>.db` must then be on the volume at the old size; it is never
   deleted. A later, normal deploy against a healthy archive runs without the flag.

## Steps, in this order

1. **Cole taps REC stop on the console** to close race session 3 (open since 2026-07-18).
2. **Dry run**, from a checkout of the branch to deploy, on the VPS:
   `pi/deploy/deploy-data-path.sh`. Read the inventory and the diff. The sessions section
   should say "no open session", and retention should go from 14 to 7.
3. **Apply**, only with Cole's go:

```bash
pi/deploy/deploy-data-path.sh --apply --expect-rotation   # the first deploy (Oct 2026)
pi/deploy/deploy-data-path.sh --apply                     # any later deploy
```

Tailscale SSH may ask for a browser approval first. `PI_HOST`, `EXPECT_BASE` and
`VERIFY_TIMEOUT_S` can be overridden from the environment.

## Rollback

```bash
pi/deploy/deploy-data-path.sh --rollback <stamp>           # dry run
pi/deploy/deploy-data-path.sh --rollback <stamp> --apply   # restore files + :pre-<stamp> image
```

The stamp is printed at the end of the deploy and is the backup folder's suffix. Rollback does
not move the corrupt archive back. The old code would then record into the fresh `archive.db`
with no disk guard, so if the point of rolling back is to stop recording, stop the archiver
instead: `cd ~/Agent_C4 && docker compose -f compose.pi.yml stop archiver`.

## Why session 3 is closed first, and why 7 days

The engine's `sessions` table aboard has session 3 ("Race 2026-07-18 18:52Z") still open. The
prune keeps every open session's window up to now + 1 day, so while it is open the prune
deletes nothing from the fresh archive.

Closing it with REC stop sets its end to the moment of the tap. That:

- deletes nothing from July or September. The prune only touches the live `archive.db`; the
  July and September data sit in rotated `archive.corrupt-*.db` files, which are never pruned.
- lets the prune work on the fresh archive: out-of-session rows go once they are 7 days old.
- records session 3 as a race from Jul 18 to the day of the tap. Anything that lists
  sessions will show that length.

Closing it after the deploy would instead keep everything recorded in between for good,
because it would sit inside session 3. That is why the tap comes first, and why `--apply`
checks for it.

Retention is 7 days on the boat (`compose.pi.yml`, Cole's call 2026-10-27; it was 14). At the
dock the archive grows ~7 GB/day: 14 days (~98 GB) can't fit in the 69 GB free, while 7 days
(~49 GB) can. The disk guard is still there for when it doesn't.
