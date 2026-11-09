#!/bin/bash
# Delete local backup files ONLY where the archived copy is proven equivalent, right now.
#
# Two proofs, both required, both taken at delete time (Cole, #243):
#   1. checksum: the LOCAL file still hashes to the sha256 in the manifest, so what is being
#      deleted is what was archived, not a file that changed underneath us since;
#   2. restore: the GCS object is read back in full, decompressed, and that restored stream
#      hashes to the same sha256. A remote object that merely exists with a non-zero size proves
#      nothing: it can be truncated, corrupt, or another file.
#
# Why a full restore-and-hash rather than e.g. `PRAGMA integrity_check` on a restored .db: equal
# sha256 means the restored copy IS the local file, byte for byte, so nothing that could be
# checked on the local file can differ on the copy. integrity_check would only say the copy is
# some valid SQLite file, not that it holds the same rows. Streaming also needs no scratch disk
# (the archived files run to 24 GB).
#
# Anything that fails is skipped, loudly, and left on disk. Exit 1 if anything was skipped.
#
# SRC / MAN / PULL may be overridden from the environment (the tests do); the defaults are the
# 2026-08-30 boat pull.
set -uo pipefail

SRC=${SRC:-/home/constantineau/backups/c4-boat-pull-2026-08-30}
MAN=${MAN:-/home/constantineau/backups/gcs-staging/MANIFEST-sha256.txt}
PULL=${PULL:-gs://constantineau-c4-archive/boat-pull-2026-08-30}
FREED=0
SKIPPED=0

skip() { echo "  [SKIP] $1"; SKIPPED=$((SKIPPED + 1)); }

for rel in "$@"; do
    f="$SRC/$rel"
    echo "=== $rel"
    [ -f "$f" ] || { skip "not on disk"; continue; }
    want=$(awk -v r="$rel" '$2 == r {print $1}' "$MAN" | head -1)
    [ -n "$want" ] || { skip "no manifest entry — never archived"; continue; }
    before=$(stat -c '%s %Y %i' "$f")
    got=$(sha256sum "$f" | awk '{print $1}')
    [ "$want" = "$got" ] || { skip "local file no longer matches the archived hash"; continue; }
    # The restore: the whole object, decompressed, hashed. pipefail makes a failed read or a
    # failed decompress fail the pipeline, whatever sha256sum printed.
    restored=$(gsutil cat "$PULL/$rel.zst" | zstd -dc | sha256sum | awk '{print $1}')
    rc=$?
    [ "$rc" -eq 0 ] || { skip "could not restore $rel.zst from GCS (rc $rc)"; continue; }
    [ "$restored" = "$got" ] || { skip "restored copy hashes to $restored, not $got"; continue; }
    [ "$(stat -c '%s %Y %i' "$f")" = "$before" ] \
        || { skip "local file changed while it was being checked"; continue; }
    bytes=$(stat -c %s "$f")
    rm -f "$f"
    FREED=$((FREED + bytes))
    echo "  [DELETED] $(numfmt --to=iec "$bytes") — restored from $rel.zst and matched, sha256 $got"
done

echo
echo "freed $(numfmt --to=iec "$FREED"), skipped $SKIPPED"
df -h "$SRC" 2>/dev/null | tail -1
[ "$SKIPPED" -eq 0 ]
