#!/bin/bash
# Archive the bench Signal K volume (sr33-pi_sk_archive) to GCS before it is wiped.
# Not pure sample data: 8.1M course-provider rows Jul 9-14 2026 + 323 n2k-socketcan.35 rows.
# Compress locally first so the upload can be integrity-checked before AND after it leaves.
set -euo pipefail

SRC=/var/lib/docker/volumes/sr33-pi_sk_archive/_data/archive.db
STAGE=/home/constantineau/backups/gcs-staging
OUT="$STAGE/sk_archive-bench-2026-09-09.db.gz"
# NOT constantineau-vps-backups: that bucket has a lifecycle rule deleting everything at 30 days,
# which is right for rotating DreamCRM dumps and fatal for an archive whose local copy is wiped.
DEST=gs://constantineau-c4-archive/bench/

echo "[1/5] source: $(ls -l "$SRC" | awk '{print $5}') bytes"
echo "[2/5] sha256 of the source (so the archive can be proven identical later)"
sha256sum "$SRC" | tee "$STAGE/sk_archive-bench-2026-09-09.db.sha256"

echo "[3/5] gzip -> $OUT"
nice -n 10 gzip -6 -c "$SRC" > "$OUT"
ls -l "$OUT"

echo "[4/5] verify the local archive (gzip -t, then the sha256 of the decompressed stream)"
gzip -t "$OUT"
gzip -dc "$OUT" | sha256sum | tee "$STAGE/roundtrip.sha256"
diff <(awk '{print $1}' "$STAGE/sk_archive-bench-2026-09-09.db.sha256") \
     <(awk '{print $1}' "$STAGE/roundtrip.sha256")
echo "    round-trip OK — the gzip decompresses to the exact source bytes"

echo "[5/5] upload (gsutil validates CRC32C end to end)"
gsutil -q cp "$OUT" "$DEST"
gsutil cp "$STAGE/sk_archive-bench-2026-09-09.db.sha256" "$DEST"
gsutil stat "${DEST}$(basename "$OUT")"
echo "DONE"
