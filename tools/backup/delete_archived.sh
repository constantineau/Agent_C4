#!/bin/bash
# Delete local backup files ONLY where the archived copy is proven equivalent, right now.
#
# The restore check already proved each GCS object decompresses to the manifest hash. This adds
# the other half: that the LOCAL file still hashes to that same value, so what is being deleted
# is what was archived — not a file that changed underneath us since. Anything that fails is
# skipped, loudly, and left on disk.
set -uo pipefail

SRC=/home/constantineau/backups/c4-boat-pull-2026-08-30
MAN=/home/constantineau/backups/gcs-staging/MANIFEST-sha256.txt
PULL=gs://constantineau-c4-archive/boat-pull-2026-08-30
FREED=0

for rel in "$@"; do
    f="$SRC/$rel"
    echo "=== $rel"
    [ -f "$f" ] || { echo "  [SKIP] not on disk"; continue; }
    want=$(awk -v r="$rel" '$2 == r {print $1}' "$MAN" | head -1)
    [ -n "$want" ] || { echo "  [SKIP] no manifest entry — never archived"; continue; }
    got=$(sha256sum "$f" | awk '{print $1}')
    [ "$want" = "$got" ] || { echo "  [SKIP] local file no longer matches the archived hash"; continue; }
    size=$(gsutil stat "$PULL/$rel.zst" 2>/dev/null | awk '/Content-Length/ {print $2}')
    [ -n "$size" ] && [ "$size" -gt 0 ] || { echo "  [SKIP] no object in GCS"; continue; }
    bytes=$(stat -c %s "$f")
    rm -f "$f"
    FREED=$((FREED + bytes))
    echo "  [DELETED] $(numfmt --to=iec "$bytes") — archived as $rel.zst ($(numfmt --to=iec "$size")), sha256 $got"
done

echo
echo "freed $(numfmt --to=iec "$FREED")"
df -h / | tail -1
