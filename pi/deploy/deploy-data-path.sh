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
#   pi/deploy/deploy-data-path.sh --apply --allow-open-session
#        # deploy even though a race session has been open for over 24 h (normally refused:
#        # an open session keeps everything since its start, so the prune frees nothing)
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
VERIFY_TIMEOUT_S=${VERIFY_TIMEOUT_S:-3600}    # quick_check of the 24 GB archive on the SD card took 39 min on 2026-10-28
COMPOSE="docker compose -f compose.pi.yml"

apply=0 rollback="" expect_rotation=0 allow_open=0
while [[ $# -gt 0 ]]; do
  case $1 in
    --apply) apply=1 ;;
    --expect-rotation) expect_rotation=1 ;;
    --allow-open-session) allow_open=1 ;;
    --rollback) rollback=${2:?--rollback needs the backup STAMP}; shift ;;
    -h|--help) sed -n 2,23p "$0"; exit 0 ;;
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

# Effective retention as the running archiver sees it, and whether the Pi's .env overrides it.
retention_now() {
  pi "docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' sr33-pi-archiver-1 | sed -n 's/^ARCHIVE_RETAIN_DAYS=//p'"
}
retention_env() { pi "cd ~/$PI_REPO && sed -n 's/^ARCHIVE_RETAIN_DAYS=//p' .env"; }

# Race sessions still open in the engine's store. Read-only: engine.db uses SQLite's default
# rollback journal (no -wal), so an immutable=1 read is exact and creates no file. If a -wal
# ever appears the check refuses to guess rather than open it in a mode that writes beside it.
# Prints "OPEN <id> <hours> <name>" lines and DONE.
open_sessions() {
  pi "docker exec -i sr33-pi-engine-1 python3 -" <<'PY'
import os, sqlite3, time
p = "/var/lib/sr33/engine/engine.db"
if os.path.exists(p + "-wal"):
    print("WAL engine.db has a -wal file; not reading it from here")
    raise SystemExit(3)
c = sqlite3.connect(f"file:{p}?immutable=1", uri=True)
now = time.time()
for i, name, start in c.execute("SELECT id, name, start_ts FROM sessions WHERE end_ts IS NULL"):
    print(f"OPEN {i} {(now - float(start)) / 3600:.0f} {name}")
print("DONE")
PY
}

# Restart the onboard engine — no rebuild, no code change — and check it answers. Needed after
# a rotation: the boat's engine (Sep 1 code) caches its archive connection and would go on
# reading the OLD file (now archive.corrupt-*.db) until restarted. dev's engine reopens on its
# own when the file is replaced; this covers the code that is actually aboard.
restart_engine() {
  echo "restarting the onboard engine so it reads the new archive.db (no rebuild):"
  pi "cd ~/$PI_REPO && $COMPOSE restart engine"
  local i
  for i in $(seq 1 12); do
    if pi "docker exec sr33-pi-engine-1 python3 -c \"import urllib.request as u; print(u.urlopen('http://localhost:8200/health', timeout=5).read().decode())\"" 2>/dev/null; then
      # /series reads the ARCHIVE (last 10 min of true wind), so it shows which file the engine
      # is on. /conditions comes from the live Signal K cache and would look fine either way.
      echo "engine archive read (/series, last 10 min):"
      pi "docker exec sr33-pi-engine-1 python3 -c \"import json, time, urllib.request as u; d = json.load(u.urlopen('http://localhost:8200/series?minutes=10', timeout=30)); p = d.get('points') or []; print('  newest archived wind %.0f s old, %d points' % (time.time() - p[-1]['t'], len(p)) if p else '  no archived wind in the last 10 min (inconclusive: true wind may simply not be reporting) - ' + str(d.get('note')))\"" \
        || echo "!! /series did not answer — look at: docker logs sr33-pi-engine-1"
      return 0
    fi
    sleep 5
  done
  echo "!! the engine did not answer /health within 60 s after its restart — look at: docker logs sr33-pi-engine-1"
  return 1
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
  if [[ $apply -eq 1 ]]; then pi "$cmd"; restart_engine || true
  else echo "(dry run) would run on the Pi: $cmd"; echo "(dry run) would restart the engine (no rebuild) and check /health"; fi
  cat <<'EOF'

Rollback restores the files and the previous image, then restarts the engine (no rebuild) so
it is reading whatever archive.db is on the volume. It does NOT move the corrupt archive back:
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

say "Race sessions (engine.db, read-only)"
session_block=""
if sess=$(open_sessions) && [[ $(tail -1 <<<"$sess") == DONE ]]; then
  stale=$(awk '$1 == "OPEN" && $3 > 24' <<<"$sess")
  grep '^OPEN' <<<"$sess" | sed 's/^OPEN \([0-9]*\) \([0-9]*\) /  open: session \1, \2 h, /' || echo "  no open session"
  if [[ -n $stale ]]; then
    session_block="a race session has been open for more than 24 h. While it is open the prune keeps everything since its start, so it frees nothing. Close it first: tap REC stop on the console."
  fi
else
  echo "$sess" | sed 's/^/  /'
  session_block="could not read the sessions table, so an open session can't be ruled out."
fi
if [[ -n $session_block ]]; then
  echo "!! $session_block"
  if [[ $allow_open -eq 0 ]]; then
    echo "!! --apply will refuse until that is fixed (or pass --allow-open-session on purpose)."
    [[ $apply -eq 1 ]] && { echo "Refusing to deploy. Nothing on the Pi was changed." >&2; exit 1; }
  else
    echo "!! --allow-open-session given: going ahead anyway."
  fi
fi

say "Retention (days of out-of-session full-res kept aboard)"
ret_now=$(retention_now || echo "?"); ret_env=$(retention_env || echo "")
ret_new=${ret_env:-$(sed -n 's/.*ARCHIVE_RETAIN_DAYS:-\([0-9.]*\)}.*/\1/p' compose.pi.yml)}
echo "  running archiver now: ${ret_now:-unset (code default)}"
echo "  after this deploy:    $ret_new  ($([[ -n $ret_env ]] && echo "set in the Pi's .env, which wins" || echo "compose.pi.yml default; the Pi's .env does not set it"))"
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
# compose.pi.yml may change only inside the services this script recreates; a change anywhere
# else would sit on the Pi unapplied until something restarted that service by surprise.
if printf '%s\n' "${changed[@]}" | grep -qx compose.pi.yml; then
  theirs=$(pi "cat ~/$PI_REPO/compose.pi.yml")
  others=$(python3 -I - "$theirs" <<'PY'
import sys, yaml
mine = yaml.safe_load(open("compose.pi.yml"))
theirs = yaml.safe_load(sys.argv[1])
skip = {"archiver", "uplink"}
diff = sorted(k for k in set(mine["services"]) | set(theirs["services"])
              if k not in skip and mine["services"].get(k) != theirs["services"].get(k))
top = sorted(k for k in set(mine) | set(theirs) if k != "services" and mine.get(k) != theirs.get(k))
print(" ".join(diff + [f"(top-level {k})" for k in top]))
print("uplink" if mine["services"].get("uplink") != theirs["services"].get("uplink") else "")
PY
)
  uplink_cfg=$(sed -n 2p <<<"$others"); others=$(sed -n 1p <<<"$others")
  if [[ -n $uplink_cfg ]] && ! printf '%s\n' "${rebuild[@]}" | grep -qx uplink; then rebuild+=(uplink); fi
  if [[ -n $others ]]; then
    echo "!! compose.pi.yml also changes: $others — this script only recreates archiver/uplink. Stopping." >&2; exit 1
  fi
  echo; echo "compose.pi.yml changes only the archiver/uplink blocks; ${rebuild[*]} will be recreated, so they apply."
fi
if printf '%s\n' "${changed[@]}" "${new[@]}" | grep -q '^pi/signalk/'; then
  echo "!! pi/signalk changed — Signal K is not restarted by this script. Stopping." >&2; exit 1
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
# Any rotation (expected or not) replaced archive.db under the engine: restart it.
if grep -q 'CORRUPT archive moved aside' <<<"$log"; then
  echo; restart_engine || echo "!! recording is fine; the engine needs a look"
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
echo "retention in the running archiver: $(retention_now)"
say "Deployed. Backup stamp: $stamp  (rollback: $0 --rollback $stamp --apply)"
