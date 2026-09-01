#!/bin/bash
# Keep the archive drain safe to leave running unattended.
#
# Three jobs, in priority order:
#   1. COMPACT. The drain writes ~76M rows all dated Aug 30 - Sep 1, so they all land in the
#      CURRENT chunk. Uncompressed that is ~20 GB against ~24 G free. The automatic
#      compression policy will NOT help here: it fires on chunks older than 2 days, and the
#      chunk covering this data does not age out until Sep 5. So compaction must be driven
#      manually while the drain runs. Compressing a chunk does not block inserts (verified:
#      live uplink kept landing at 5.5s lag into a compressed chunk).
#   2. GUARD. If headroom still collapses, stop the drain on the Pi rather than let Postgres
#      hit a full disk. The drain is cursor-based, so stopping is free — it resumes exactly.
#   3. REPORT. Append a status line so the next session can see progress without re-deriving.
#
# Run detached (setsid) so it outlives the shell that started it.
LOG=/home/constantineau/backups/drain-guardian.log
PSQL="docker exec sr33-dev-timescaledb-1 psql -U sr33 -d sr33_dev -tAc"
PI=sr33-pi@100.79.180.102
MIN_FREE_KB=5000000        # 5 G — below this, stop the drain

echo "=== $(date -u +%FT%TZ) guardian started ===" >> $LOG

while true; do
  # 1. compact every chunk with uncompressed rows. One statement per chunk in its own
  #    transaction: a multi-chunk statement deadlocks against live uplink inserts.
  for c in $($PSQL "select show_chunks('telemetry_raw');" 2>/dev/null); do
    $PSQL "select compress_chunk('$c', if_not_compressed => true);" >/dev/null 2>&1
  done

  rows=$($PSQL "select count(*) from telemetry_raw;" 2>/dev/null)
  size=$($PSQL "select pg_size_pretty(hypertable_size('telemetry_raw'));" 2>/dev/null)
  avail=$(df / | tail -1 | awk '{print $4}')
  free=$(df -h / | tail -1 | awk '{print $4}')
  pos=$(timeout 25 ssh -o ConnectTimeout=10 -o BatchMode=yes $PI \
          'tail -1 /tmp/drain-archive.log' 2>/dev/null | tr -d '\r')
  echo "$(date -u +%FT%TZ) rows=$rows size=$size free=$free | $pos" >> $LOG

  # 2. guard
  if [ -n "$avail" ] && [ "$avail" -lt "$MIN_FREE_KB" ]; then
    echo "$(date -u +%FT%TZ) !! FREE DISK UNDER 5G — STOPPING DRAIN ON THE PI !!" >> $LOG
    timeout 30 ssh -o ConnectTimeout=10 -o BatchMode=yes $PI \
      'pkill -f drain-archive.sh; pkill -f backfill.py' >/dev/null 2>&1
    echo "$(date -u +%FT%TZ) drain stopped; cursor preserved, resume with /tmp/drain-archive.sh" >> $LOG
    exit 1
  fi

  # 3. stop cleanly once the drain reports done
  if timeout 25 ssh -o ConnectTimeout=10 -o BatchMode=yes $PI \
       'grep -q "DRAIN COMPLETE" /tmp/drain-archive.log' 2>/dev/null; then
    echo "$(date -u +%FT%TZ) DRAIN COMPLETE — final compaction pass" >> $LOG
    for c in $($PSQL "select show_chunks('telemetry_raw');" 2>/dev/null); do
      $PSQL "select compress_chunk('$c', if_not_compressed => true);" >/dev/null 2>&1
    done
    rows=$($PSQL "select count(*) from telemetry_raw;" 2>/dev/null)
    size=$($PSQL "select pg_size_pretty(hypertable_size('telemetry_raw'));" 2>/dev/null)
    echo "$(date -u +%FT%TZ) FINAL rows=$rows size=$size free=$(df -h / | tail -1 | awk '{print $4}')" >> $LOG
    exit 0
  fi

  sleep 300
done
