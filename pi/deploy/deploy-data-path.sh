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

apply=0 rollback=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --apply) apply=1 ;;
    --rollback) rollback=${2:?--rollback needs the backup STAMP}; shift ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
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
# One ssh for every checksum; files missing on the Pi print nothing and count as new.
declare -A pisum
while read -r sum f; do pisum[$f]=$sum; done < <(pi "cd ~/$PI_REPO && sha256sum ${all[*]} 2>/dev/null" || true)
changed=() new=()
for f in "${all[@]}"; do
  here=$(sha256sum "$f" | cut -d' ' -f1)
  if [[ -z ${pisum[$f]:-} ]]; then new+=("$f")
  elif [[ ${pisum[$f]} != "$here" ]]; then changed+=("$f"); fi
done
dirty=$(pi "cd ~/$PI_REPO && git status --short -- ${all[*]}" || true)
if [[ -n $dirty ]]; then
  echo "The Pi has LOCAL edits to data-path files — stop and look before overwriting them:"; echo "$dirty"; exit 1
fi
if [[ ${#changed[@]} -eq 0 && ${#new[@]} -eq 0 ]]; then echo "Nothing differs. The Pi already runs these files."; exit 0; fi
echo "changed: ${changed[*]:-none}"
echo "new:     ${new[*]:-none}"
for f in "${changed[@]}"; do
  echo; pi "cat ~/$PI_REPO/$f" | diff -u --label "pi:$f" --label "here:$f" - "$f" || true
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
say "Verify (up to ${VERIFY_TIMEOUT_S}s for the startup check of the old archive)"
start=$(date +%s); seen=""
while (( $(date +%s) - start < VERIFY_TIMEOUT_S )); do
  log=$(pi "docker logs --since '$((($(date +%s) - start) + 60))s' sr33-pi-archiver-1 2>&1" || true)
  if grep -q 'CORRUPT archive moved aside' <<<"$log" && grep -q '+[0-9]* rows (total' <<<"$log"; then seen=1; break; fi
  if grep -q 'ready (' <<<"$log" && ! grep -q 'recovered from' <<<"$log"; then
    echo "!! the archiver opened archive.db WITHOUT rotating it. That file failed count(*) for"
    echo "!! eight weeks; recording into it is not what we want. Stopping it — look before retrying:"
    pi "cd ~/$PI_REPO && $COMPOSE stop archiver"; exit 1
  fi
  sleep 20
done
pi "docker logs --since 40m sr33-pi-archiver-1 2>&1 | grep -E 'unusable|count\(\*\) failed|CORRUPT|ready \(|disk guard|!!!' | tail -8"
[[ -n $seen ]] || { echo "!! no rotation + first write seen within ${VERIFY_TIMEOUT_S}s — check the log above"; exit 1; }

echo; echo "archive volume (expect archive.corrupt-<stamp>.db = the old 24 GB file, and a small new archive.db):"
archive_ls | sed 's/^/  /'
db_bytes() {   # archive.db + its WAL (fresh writes sit in the WAL until a checkpoint)
  pi "docker exec sr33-pi-engine-1 sh -c 'stat -c %s /var/lib/sr33/archive/archive.db /var/lib/sr33/archive/archive.db-wal 2>/dev/null; true'" \
    | awk '{s += $1} END {print s + 0}'
}
s1=$(db_bytes); sleep 60; s2=$(db_bytes)
echo "fresh archive (db + wal): $s1 -> $s2 bytes in 60 s"; (( s2 > s1 )) || echo "!! not growing"
echo; echo "status file:"; pi "docker exec sr33-pi-engine-1 cat /var/lib/sr33/archive/archiver-status.json" || echo "!! no status file yet"
echo; echo -n "uplink sends in the last 2 min: "
pi "docker logs --since 2m sr33-pi-uplink-1 2>&1 | grep -c 'sent'" || echo "!! uplink quiet"
echo -n "archiver restarts since recreate: "; pi "docker inspect -f '{{.RestartCount}}' sr33-pi-archiver-1"
say "Deployed. Backup stamp: $stamp  (rollback: $0 --rollback $stamp --apply)"
