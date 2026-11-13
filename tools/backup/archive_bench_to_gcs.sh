#!/bin/bash
# Archive the bench Signal K volume (<project>_sk_archive) to GCS before it is wiped.
#
#   STAGE=<local staging dir> DEST=gs://<bucket>/bench/ tools/backup/archive_bench_to_gcs.sh
#
# STAGE and DEST have no default. Runs on the bench host: the volume is <bench project>_sk_archive,
# the project being C4_BENCH_PROJECT (exported, else in this checkout's .env; default c4-bench, the
# compose.pi.sample.yml default, #314). SRC overrides the whole path.
# Not pure sample data: 8.1M course-provider rows Jul 9-14 2026 + 323 n2k-socketcan.35 rows.
# Compress locally first so the upload can be integrity-checked before AND after it leaves.
set -euo pipefail

if [[ -z ${SRC:-} && -z ${C4_BENCH_PROJECT:-} && -f "$(dirname "$0")/../../.env" ]]; then
  C4_BENCH_PROJECT=$(sed -n 's/^C4_BENCH_PROJECT=//p' "$(dirname "$0")/../../.env" | tail -1 | tr -d "\"'")
fi
SRC=${SRC:-/var/lib/docker/volumes/${C4_BENCH_PROJECT:-c4-bench}_sk_archive/_data/archive.db}
STAGE=${STAGE:?set STAGE to a local staging directory}
OUT="$STAGE/sk_archive-bench-2026-09-09.db.gz"
# NOT a bucket with a lifecycle rule that deletes at 30 days (the VPS backups bucket): right for
# rotating dumps, fatal for an archive whose local copy is wiped. Use the archive bucket.
DEST=${DEST:?set DEST to gs://<archive bucket>/bench/}

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
