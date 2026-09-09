#!/bin/bash
# Archive the 2026-08-30 boat pull to Coldline. This is the only copy of the Jul 18 race record
# that exists anywhere: the corrupt original salvage.py recovered 99.9994% of, the Jul 15-17
# delivery salvage, the replay rig's archive, and the engine store whose sessions table settled
# what happened to Jul 18's out-of-session hours.
#
# Nothing is deleted here. Every file is hashed, compressed, decompressed again and re-hashed
# against the source before it is uploaded, and the manifest of source hashes goes up with it —
# so the archive can be PROVEN identical to what was on this disk, not merely assumed to be.
set -euo pipefail

SRC=/home/constantineau/backups/c4-boat-pull-2026-08-30
STAGE=/home/constantineau/backups/gcs-staging
DEST=gs://constantineau-c4-archive/boat-pull-2026-08-30
MANIFEST="$STAGE/MANIFEST-sha256.txt"

BIG=(
  pi/sk_archive/archive.corrupt-20260718.db
  recovered/archive-jul1517-recovered.db
  work/archive-backfill.db
  pi/sk_archive/archive.db
  recovered/archive-recovered.db
)

: > "$MANIFEST"

for rel in "${BIG[@]}"; do
    f="$SRC/$rel"
    out="$STAGE/$(basename "$rel").zst"
    echo "=== $rel ($(stat -c %s "$f") bytes)"
    src_hash=$(sha256sum "$f" | awk '{print $1}')
    echo "$src_hash  $rel" >> "$MANIFEST"
    nice -n 10 zstd -T6 -3 -q -f -o "$out" "$f"
    zstd -t "$out"
    back=$(zstd -dc "$out" | sha256sum | awk '{print $1}')
    [ "$src_hash" = "$back" ] || { echo "ROUND-TRIP MISMATCH on $rel — NOT uploading"; exit 1; }
    echo "    round-trip OK  $(stat -c %s "$f") -> $(stat -c %s "$out") bytes"
    gsutil -q cp "$out" "$DEST/$(dirname "$rel")/"
    rm -f "$out"
    echo "    uploaded to $DEST/$rel.zst"
done

# Everything else: 6,361 files, 0.88 G — one tarball, verified against the filesystem itself.
echo "=== the remaining 6,361 files (orin journal, configs, engine.db, sk_queue)"
REST="$STAGE/rest-of-pull.tar.zst"
tar -C "$SRC" -cf - --exclude="${BIG[0]}" --exclude="${BIG[1]}" --exclude="${BIG[2]}" \
    --exclude="${BIG[3]}" --exclude="${BIG[4]}" . | nice -n 10 zstd -T6 -3 -q -f -o "$REST"
zstd -t "$REST"
zstd -dc "$REST" | tar -C "$SRC" --diff -f - > "$STAGE/tar-diff.txt" 2>&1 || true
if [ -s "$STAGE/tar-diff.txt" ]; then
    echo "TAR DIFFERS FROM DISK — NOT uploading:"; head -20 "$STAGE/tar-diff.txt"; exit 1
fi
echo "    tar --diff against the live tree is clean"
find "$SRC" -type f -size -1073741824c -exec sha256sum {} + >> "$MANIFEST"
gsutil -q cp "$REST" "$DEST/"
rm -f "$REST"

gsutil cp "$MANIFEST" "$DEST/"
echo "=== uploaded:"
gsutil du -h "$DEST"
echo "DONE"
