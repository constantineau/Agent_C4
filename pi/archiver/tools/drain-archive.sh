#!/bin/sh
# Stream the Pi's live full-resolution archive.db to the VPS — self-healing.
#
# Nothing is copied: backfill.py --all runs inside the archiver container, which already has
# the DB and the network, so the 16 GB file never leaves the boat.
#
# Two distinct failure modes, and they need different responses:
#   1. transient — a read racing the archiver's WAL writes. Just retrying works.
#   2. a genuinely corrupt page. Retrying NEVER works: backfill pages with
#      `id > cursor ORDER BY id LIMIT n`, so a corrupt page inside that window makes the
#      whole SELECT raise and the cursor can never advance. The drain wedges forever
#      (observed at ids 470500..471000).
# So on repeated failure, hand off to step_over_bad.py, which bisects the next window down to
# single rows, recovers everything readable, and nudges the cursor past only what is truly
# unreadable. Losing 46 rows beats losing the 79.5M behind them.
#
# Resumable throughout: backfill.py's `backfill_last_id` cursor lives in the archive's
# sync_state, and telemetry_raw has no PK, so never re-send — only ever advance.
LOG=/tmp/drain-archive.log
LOST=/tmp/drain-lost-rowids.log
VPS=http://100.88.252.115:8101      # the container's baked-in VPS_URL is stale/unreachable
STRIDE=3000

cursor() {
  docker exec sr33-pi-archiver-1 python -c "
import sqlite3
c=sqlite3.connect('file:/var/lib/sr33/archive/archive.db?mode=ro',uri=True,timeout=30)
print(c.execute(\"select v from sync_state where k='backfill_last_id'\").fetchone()[0])" 2>/dev/null
}

echo "=== $(date -u +%FT%TZ) drain starting ===" >> $LOG
fails=0
for i in $(seq 1 2000); do
  docker exec -e VPS_URL=$VPS -e BACKFILL_BATCH=3000 \
    sr33-pi-archiver-1 python /app/backfill.py --all >> $LOG 2>&1
  if [ $? -eq 0 ]; then
    echo "=== $(date -u +%FT%TZ) DRAIN COMPLETE (attempt $i) ===" >> $LOG
    exit 0
  fi

  fails=$((fails + 1))
  echo "=== $(date -u +%FT%TZ) attempt $i failed (consecutive=$fails) ===" >> $LOG

  # Two consecutive failures means it is not a transient WAL race — bisect past the page.
  if [ $fails -ge 2 ]; then
    cur=$(cursor)
    if [ -n "$cur" ]; then
      target=$((cur + STRIDE))
      echo "--- $(date -u +%FT%TZ) stepping over corrupt region: $cur -> $target ---" >> $LOG
      docker exec -e VPS_URL=$VPS sr33-pi-archiver-1 \
        python /tmp/step_over_bad.py $target >> $LOG 2>&1
      docker exec sr33-pi-archiver-1 sh -c "grep -h UNREADABLE /tmp/step_last.txt" >> $LOST 2>/dev/null
    fi
    fails=0
  fi
  sleep 5
done
echo "=== $(date -u +%FT%TZ) GAVE UP ===" >> $LOG
exit 1
