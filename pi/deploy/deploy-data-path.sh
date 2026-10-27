#!/usr/bin/env bash
# Deploy the data path (archiver + its shared/ modules; uplink only if its files changed) from
# THIS checkout to the boat's Pi, the way CLAUDE.md says a Pi deploy works: diff, back up, copy
# single files, rebuild only the changed services. Never `git pull` on the Pi — its clone sits
# on `main` and a pull there switches what every other service runs.
#
# DRY RUN BY DEFAULT: inventories the Pi and shows the exact diff, changes nothing.
#
#   pi/deploy/deploy-data-path.sh                    # dry run: inventory + diff + the plan
#   pi/deploy/deploy-data-path.sh --apply            # back up, copy, rebuild, verify
#   pi/deploy/deploy-data-path.sh --apply --expect-rotation
#        # the FIRST deploy after the 2026-09-02 corruption: the archiver must move the corrupt
#        # archive.db aside; opening it without a rotation stops the archiver. Without the flag
#        # a healthy open is normal and only successful writes are checked.
#   pi/deploy/deploy-data-path.sh --rollback STAMP            # dry run of a rollback
#   pi/deploy/deploy-data-path.sh --rollback STAMP --apply    # restore files + previous image
#
# Starting the new archiver restarts full-res recording on the boat. That is Cole's call: do
# not pass --apply without his go. See pi/deploy/README.md.
set -euo pipefail

PI_HOST=${PI_HOST:-sr33-pi@100.79.180.102}
PI_REPO=${PI_REPO:-Agent_C4}                  # relative to the Pi user's home
BACKUPS=${BACKUPS:-deploy-backups}            # relative to the Pi user's home
EXPECT_BASE=${EXPECT_BASE:-b3c30a1}           # what the Pi's clone should be on
VERIFY_TIMEOUT_S=${VERIFY_TIMEOUT_S:-1800}    # quick_check of a 24 GB file on an SD card is slow
COMPOSE="docker compose -f compose.pi.yml"

apply=0 rollback="" expect_rotation=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --apply) apply=1 ;;
    --expect-rotation) expect_rotation=1 ;;
    --rollback) rollback=${2:?--rollback needs the backup STAMP}; shift ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

cd "$(git rev-parse --show-toplevel)"
say()  { printf '\n=== %s\n' "$*"; }
pi()   { ssh -o ConnectTimeout=15 "$PI_HOST" "$@"; }
# Read-only look inside the archive volume, through the engine container (it mounts the volume
# read-only), so no new container is started on the Pi just to list a directory.
archive_ls() { pi "docker exec sr33-pi-engine-1 ls -la --time-style=+%FT%T /var/lib/sr33/archive"; }
run() { if [[ $apply -eq 1 ]]; then echo "+ $*"; "$@"; else echo "(dry run) would run: $*"; fi; }

# The files that make up the data path. Tests are not shipped; they run in `make ci` here.
files() {
  git ls-files pi/archiver pi/uplink pi/signalk shared compose.pi.yml | grep -v '/test_' || true
}

inventory() {
  say "Pi inventory ($PI_HOST) — read-only"
  pi "cd ~/$PI_REPO && echo \"branch: \$(git branch --show-current)  head: \$(git log -1 --format='%h %ad %s' --date=short)\" \
      && echo 'local changes:' && git status --short | sed 's/^/  /'; \
      echo; docker ps -a --format '{{.Names}}\t{{.Status}}' | sort; \
      echo; echo -n 'archiver restarts: '; docker inspect -f '{{.RestartCount}}' sr33-pi-archiver-1 2>/dev/null || echo '?'; \
      echo; df -h / | tail -1; free -h | sed -n 2p"
  echo; echo "archive volume:"; archive_ls | sed 's/^/  /'
  echo; echo "archiver log tail:"; pi "docker logs --tail 4 sr33-pi-archiver-1 2>&1" | sed 's/^/  /'
}

if [[ -n $rollback ]]; then
  bdir="$BACKUPS/data-path-$rollback"
  say "ROLLBACK to $bdir"
  pi "test -d ~/$bdir" || { echo "no such backup on the Pi: ~/$bdir" >&2; exit 1; }
  pi "cat ~/$bdir/manifest.txt"
  cmd="set -e; cd ~/$PI_REPO \
    && tar -xf ~/$bdir/files.tar \
    && if [ -s ~/$bdir/new-files.txt ]; then xargs -r rm -f < ~/$bdir/new-files.txt; fi \
    && docker tag sr33-pi-archiver:pre-$rollback sr33-pi-archiver \
    && if docker image inspect sr33-pi-uplink:pre-$rollback >/dev/null 2>&1 && grep -q '^uplink$' ~/$bdir/rebuilt.txt; then docker tag sr33-pi-uplink:pre-$rollback sr33-pi-uplink && $COMPOSE up -d --no-deps --no-build uplink; fi \
    && $COMPOSE up -d --no-deps --no-build archiver"
  if [[ $apply -eq 1 ]]; then pi "$cmd"; else echo "(dry run) would run on the Pi: $cmd"; fi
  cat <<'EOF'

Rollback restores the files and the previous image. It does NOT move the corrupt archive back:
archive.corrupt-<stamp>.db stays where it is, and the old code will start recording into the
fresh archive.db, without the disk guard. To stop full-res recording instead:
  ssh $PI_HOST 'cd ~/Agent_C4 && docker compose -f compose.pi.yml stop archiver'
EOF
  exit 0
fi

# ---------------------------------------------------------------------------------- 1. inventory
echo "local: $(git rev-parse --abbrev-ref HEAD) @ $(git log -1 --format='%h %s')"
[[ -z $(git status --porcelain -- $(files)) ]] || { echo "local data-path files have uncommitted changes — commit first" >&2; exit 1; }
inventory
pi_head=$(pi "cd ~/$PI_REPO && git rev-parse --short=7 HEAD")
[[ $pi_head == "$EXPECT_BASE"* ]] || echo "WARNING: the Pi is on $pi_head, not $EXPECT_BASE — read the diff below carefully"

# -------------------------------------------------------------------------------------- 2. diff
say "What differs: the Pi's ~/$PI_REPO vs this checkout"
mapfile -t all < <(files)
# One ssh for every checksum. Each file answers "SUM <hash> <path>" or "MISSING <path>", and
# the listing must end with DONE: a failed ssh, or one cut short, aborts the deploy rather than
# reading as "every file is new" — which would leave real files out of the backup and have a
# rollback delete them.
remote_sums=$(pi "cd ~/$PI_REPO && for f in ${all[*]}; do \
    if [ -e \"\$f\" ]; then h=\$(sha256sum \"\$f\") || exit 1; echo \"SUM \${h%% *} \$f\"; \
    else echo \"MISSING \$f\"; fi; done && echo DONE") \
  || { echo "!! could not read the Pi's files (ssh or checksum failed) — aborting, nothing changed" >&2; exit 1; }
[[ $(tail -1 <<<"$remote_sums") == DONE ]] \
  || { echo "!! the Pi's file listing was incomplete — aborting, nothing changed" >&2; exit 1; }
declare -A pisum pimissing
while read -r kind a b; do
  case $kind in
    SUM) [[ -n $a && -n $b ]] && pisum[$b]=$a ;;
    MISSING) pimissing[$a]=1 ;;
  esac
done <<<"$remote_sums"
changed=() new=()
for f in "${all[@]}"; do
  here=$(sha256sum "$f" | cut -d' ' -f1)
  if [[ -n ${pimissing[$f]:-} ]]; then new+=("$f")
  elif [[ -z ${pisum[$f]:-} ]]; then echo "!! no checksum and no MISSING for $f — aborting" >&2; exit 1
  elif [[ ${pisum[$f]} != "$here" ]]; then changed+=("$f"); fi
done
dirty=$(pi "cd ~/$PI_REPO && git status --short -- ${all[*]}") \
  || { echo "!! could not check the Pi's clone for local edits — aborting, nothing changed" >&2; exit 1; }
if [[ -n $dirty ]]; then
  echo "The Pi has LOCAL edits to data-path files — stop and look before overwriting them:"; echo "$dirty"
  echo "(An earlier run of this script leaves exactly this state; its backups are in ~/$BACKUPS on the Pi.)"; exit 1
fi
if [[ ${#changed[@]} -eq 0 && ${#new[@]} -eq 0 ]]; then echo "Nothing differs. The Pi already runs these files."; exit 0; fi
echo "changed: ${changed[*]:-none}"
echo "new:     ${new[*]:-none}"
for f in "${changed[@]}"; do
  theirs=$(pi "cat ~/$PI_REPO/$f") || { echo "!! could not read $f on the Pi — aborting" >&2; exit 1; }
  echo; diff -u --label "pi:$f" --label "here:$f" <(printf '%s\n' "$theirs") "$f" || true
done
for f in "${new[@]}"; do echo; echo "--- new file: $f ($(wc -l < "$f") lines)"; done

rebuild=(archiver)
for f in "${changed[@]}" "${new[@]}"; do
  [[ $f == pi/uplink/* ]] && { rebuild+=(uplink); break; }
done
if printf '%s\n' "${changed[@]}" "${new[@]}" | grep -qE '^(compose\.pi\.yml|pi/signalk/)'; then
  echo; echo "NOTE: compose.pi.yml or pi/signalk changed. This script rebuilds only ${rebuild[*]};"
  echo "      anything else those changes touch needs its own look before it is restarted."
fi
echo; echo "will rebuild: ${rebuild[*]}   (the uplink's code is unchanged unless listed here)"

# ------------------------------------------------------------------------------------ 3. backup
stamp=$(date -u +%Y%m%dT%H%M%SZ)
bdir="$BACKUPS/data-path-$stamp"
say "Back up to the Pi's ~/$bdir"
backup="set -e; mkdir -p ~/$bdir && cd ~/$PI_REPO \
  && tar -cf ~/$bdir/files.tar -T /dev/null ${changed[*]:-} \
  && printf '%s\n' ${new[*]:-} > ~/$bdir/new-files.txt \
  && printf '%s\n' ${rebuild[*]} > ~/$bdir/rebuilt.txt \
  && { echo \"backup $stamp of \$(git log -1 --format='%h %s')\"; echo 'changed: ${changed[*]:-}'; echo 'new: ${new[*]:-}'; } > ~/$bdir/manifest.txt"
for svc in "${rebuild[@]}"; do backup+=" && docker tag sr33-pi-$svc sr33-pi-$svc:pre-$stamp"; done
if [[ $apply -eq 1 ]]; then pi "$backup"; else echo "(dry run) would run on the Pi: $backup"; fi

# -------------------------------------------------------------------------------------- 4. copy
say "Copy ${#changed[@]} changed + ${#new[@]} new file(s)"
if [[ $apply -eq 1 ]]; then
  tar -cf - "${changed[@]}" "${new[@]}" | pi "cd ~/$PI_REPO && tar -xvf -"
else
  echo "(dry run) would run: tar -cf - <files> | ssh $PI_HOST 'cd ~/$PI_REPO && tar -xvf -'"
fi

# ------------------------------------------------------------------------------------ 5. rebuild
say "Rebuild and restart ONLY: ${rebuild[*]}"
restart="set -e; cd ~/$PI_REPO && $COMPOSE build ${rebuild[*]} && $COMPOSE up -d --no-deps ${rebuild[*]}"
if [[ $apply -eq 1 ]]; then pi "$restart"; else echo "(dry run) would run on the Pi: $restart"; fi

if [[ $apply -eq 0 ]]; then
  say "Dry run done — nothing on the Pi was changed. Re-run with --apply (Cole's go) to deploy."
  exit 0
fi

# ------------------------------------------------------------------------------------- 6. verify
say "Verify (up to ${VERIFY_TIMEOUT_S}s for the startup check of the archive)"
start=$(date +%s); seen=""
while (( $(date +%s) - start < VERIFY_TIMEOUT_S )); do
  log=$(pi "docker logs --since '$((($(date +%s) - start) + 60))s' sr33-pi-archiver-1 2>&1" || true)
  wrote=$(grep -c '+[0-9]* rows (total' <<<"$log" || true)
  if [[ $expect_rotation -eq 0 ]]; then
    # A normal deploy: a healthy open is expected; success is the archiver writing.
    if grep -q 'ready (' <<<"$log" && (( wrote > 0 )); then seen=1; break; fi
  elif grep -q 'CORRUPT archive moved aside' <<<"$log" && (( wrote > 0 )); then seen=1; break
  elif grep -q 'ready (' <<<"$log" && ! grep -q 'recovered from' <<<"$log"; then
    echo "!! the archiver opened archive.db WITHOUT rotating it. That file failed count(*) for"
    echo "!! eight weeks; recording into it is not what we want. Stopping it — look before retrying:"
    pi "cd ~/$PI_REPO && $COMPOSE stop archiver"; exit 1
  fi
  sleep 20
done
pi "docker logs --since 40m sr33-pi-archiver-1 2>&1 | grep -E 'unusable|count\(\*\) failed|CORRUPT|ready \(|disk guard|!!!' | tail -8"
if [[ -z $seen ]]; then
  if [[ $expect_rotation -eq 1 ]]; then what="rotation + first write"; else what="open + first write"; fi
  echo "!! no $what seen within ${VERIFY_TIMEOUT_S}s — check the log above"; exit 1
fi

echo; echo "archive volume:"
[[ $expect_rotation -eq 1 ]] && echo "(expect archive.corrupt-<stamp>.db = the old 24 GB file, and a small new archive.db)"
archive_ls | sed 's/^/  /'
db_bytes() {   # archive.db + its WAL (fresh writes sit in the WAL until a checkpoint)
  pi "docker exec sr33-pi-engine-1 sh -c 'stat -c %s /var/lib/sr33/archive/archive.db /var/lib/sr33/archive/archive.db-wal 2>/dev/null; true'" \
    | awk '{s += $1} END {print s + 0}'
}
s1=$(db_bytes); sleep 60; s2=$(db_bytes)
echo "archive (db + wal): $s1 -> $s2 bytes in 60 s"; (( s2 > s1 )) || echo "!! not growing"
echo; echo "status file:"; pi "docker exec sr33-pi-engine-1 cat /var/lib/sr33/archive/archiver-status.json" || echo "!! no status file yet"
echo; echo -n "uplink sends in the last 2 min: "
pi "docker logs --since 2m sr33-pi-uplink-1 2>&1 | grep -c 'sent'" || echo "!! uplink quiet"
echo -n "archiver restarts since recreate: "; pi "docker inspect -f '{{.RestartCount}}' sr33-pi-archiver-1"
say "Deployed. Backup stamp: $stamp  (rollback: $0 --rollback $stamp --apply)"
