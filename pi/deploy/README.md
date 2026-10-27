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
2. **Diff.** Every data-path file (`pi/archiver`, `pi/uplink`, `pi/signalk`, `shared`,
   `compose.pi.yml`; tests excluded) is checksummed on the Pi and here. Changed files are shown
   as a unified diff, new files by name. It stops if the Pi has local edits to any of them.
3. **Backup** to `~/deploy-backups/data-path-<stamp>/` on the Pi: a tar of the files about to
   be overwritten, the list of new files, and the current images tagged `:pre-<stamp>`.
4. **Copy** the changed and new files into `~/Agent_C4`.
5. **Rebuild and restart only** the archiver (and the uplink if its files changed):
   `docker compose -f compose.pi.yml build archiver && … up -d --no-deps archiver`.
   Signal K, the engine, the console and n2kout are not touched.
6. **Verify.** Waits (up to 30 min; checking a 24 GB file on the SD card is slow) for the
   archiver to log the rotation of the old file and its first write. If it opened the old
   file without rotating it, the script stops the archiver and exits. Then it lists the volume
   (`archive.corrupt-<stamp>.db` must be there at the old size — it is never deleted), checks
   the fresh `archive.db` grows over 60 s, prints `archiver-status.json`, and checks the uplink
   is still sending.

## Steps

```bash
# from a checkout of the branch to deploy, on the VPS
pi/deploy/deploy-data-path.sh              # dry run: read the inventory and the diff
pi/deploy/deploy-data-path.sh --apply      # only with Cole's go
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

## Before the first data-path deploy (2026-10): race session 3

The engine's `sessions` table aboard still has session 3 ("Race 2026-07-18 18:52Z") open:
`end_ts` is NULL. The prune keeps every open session's window up to now + 1 day, so **while it
is open the prune deletes nothing from the fresh archive**. The disk guard is then the only
thing between ~7 GB/day and a full card: with 69 GB free and the guard at 85% of 115 GB it
stops full-res after roughly 8 days at the dock.

Closing it (the console's REC stop sets `end_ts` to the moment of the tap):

- deletes nothing from July. The prune only touches the live `archive.db`; the July and
  September data sit in rotated `archive.corrupt-*.db` files, which are never pruned.
- makes everything recorded after the close prunable once it is 14 days old.
- keeps everything recorded between the deploy and the close for good (it is inside session
  3). Close it **before** the deploy so the fresh archive starts outside any session.
- records session 3 as a race from Jul 18 to the day of the tap. Anything that lists
  sessions will show that length. Setting its real end (~Jul 19 00:25Z) instead means editing
  `engine.db` by hand.
- does not by itself make the numbers fit: 14 days × ~7 GB is ~98 GB, more than the 69 GB free.
  With retention working the guard still trips at the dock; a shorter `ARCHIVE_RETAIN_DAYS`
  (7 days ≈ 49 GB) would fit.
