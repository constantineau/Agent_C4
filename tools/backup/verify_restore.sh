#!/bin/bash
# Prove the GCS archive RESTORES before anything local is deleted.
#
# An upload that returned 0 is not a backup — it is a claim about a backup. Every object is
# streamed back out of Coldline, decompressed in the pipe (no local disk), and hashed against the
# sha256 taken from the ORIGINAL file before it ever went up. A mismatch here means the local copy
# is still the only good copy.
set -uo pipefail

BUCKET=gs://constantineau-c4-archive
PULL="$BUCKET/boat-pull-2026-08-30"
SRC=/home/constantineau/backups/c4-boat-pull-2026-08-30
MAN=/home/constantineau/backups/gcs-staging/MANIFEST-sha256.txt
fail=0

echo "=== restoring each large object from Coldline and hashing the stream"
for rel in pi/sk_archive/archive.corrupt-20260718.db \
           recovered/archive-jul1517-recovered.db \
           work/archive-backfill.db \
           pi/sk_archive/archive.db \
           recovered/archive-recovered.db; do
    want=$(awk -v r="$rel" '$2 == r {print $1}' "$MAN" | head -1)
    got=$(gsutil cat "$PULL/$rel.zst" | zstd -dc | sha256sum | awk '{print $1}')
    if [ -n "$want" ] && [ "$want" = "$got" ]; then
        echo "  [OK ] $rel  $got"
    else
        echo "  [FAIL] $rel  want=$want got=$got"; fail=1
    fi
done

echo "=== restoring the tarball and hashing every file inside it"
TMP=$(mktemp -d /home/constantineau/backups/gcs-staging/restore.XXXX)
gsutil cat "$PULL/rest-of-pull.tar.zst" | zstd -dc | tar -C "$TMP" -xf -
n=0; bad=0
while read -r want path; do
    case "$path" in "$SRC"/*) ;; *) continue ;; esac
    rest="${path#$SRC/}"
    [ -f "$TMP/$rest" ] || { echo "  [FAIL] missing from the tarball: $rest"; bad=$((bad+1)); continue; }
    got=$(sha256sum "$TMP/$rest" | awk '{print $1}')
    [ "$want" = "$got" ] || { echo "  [FAIL] $rest"; bad=$((bad+1)); }
    n=$((n+1))
done < "$MAN"
echo "  checked $n restored files, $bad bad"
[ "$bad" -eq 0 ] || fail=1
rm -rf "$TMP"

echo "=== restoring the bench archive (its local copy is ALREADY gone)"
want=$(awk '{print $1}' /home/constantineau/backups/gcs-staging/sk_archive-bench-2026-09-09.db.sha256)
got=$(gsutil cat "$BUCKET/bench/sk_archive-bench-2026-09-09.db.gz" | gzip -dc | sha256sum | awk '{print $1}')
if [ "$want" = "$got" ]; then echo "  [OK ] bench archive restores to $got"
else echo "  [FAIL] bench archive: want=$want got=$got"; fail=1; fi

echo
[ "$fail" -eq 0 ] && echo "ALL RESTORES VERIFIED" || echo "RESTORE VERIFICATION FAILED — DELETE NOTHING"
exit "$fail"
