#!/usr/bin/env bash
# Switch a box's systemd units from the old sr33-* names to c4-* (#314 PR D). Run from a clean
# checkout of what the box runs; the unit files come from this checkout's HEAD.
#
# DRY RUN BY DEFAULT: shows the box's units and the plan, changes nothing.
#
#   pi/deploy/switch-units.sh <user@host> orin|pi             # dry run
#   pi/deploy/switch-units.sh <user@host> orin|pi --apply     # back up, switch, check
#   pi/deploy/switch-units.sh <user@host> --rollback units-STAMP [--apply]
#
# Order: the Orin first needs its checkout on a commit with this PR (the shed guard and its
# readers move to /run/c4-shed together). The Pi needs nothing else: only can0 changes there, and
# it keeps running; the c4-can0 name takes over at the next boot.
#
# Each run copies pi/systemd and the box-side script to ~/c4-unit-switch/<commit> on the box and
# runs it with sudo. Backups go to ~/deploy-backups/units-STAMP on the box.
set -euo pipefail

HOST=${1:-}
[[ -n $HOST && $HOST != -* ]] || { echo "usage: switch-units.sh <user@host> orin|pi [--apply]" >&2; exit 2; }
shift
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST")

top=$(git rev-parse --show-toplevel)
cd "$top"
sha=$(git rev-parse --short HEAD)

if [[ ${1:-} == --rollback ]]; then
  [[ -n ${2:-} ]] || { echo "usage: switch-units.sh <user@host> --rollback units-STAMP [--apply]" >&2; exit 2; }
  [[ $2 =~ ^units-[0-9TZ]+$ ]] || { echo "not a backup name: $2" >&2; exit 2; }
  "${SSH[@]}" "test -f ~/c4-unit-switch/$sha/pi/deploy/switch-units-box.sh" \
    || git archive HEAD pi/deploy/switch-units-box.sh pi/systemd \
       | "${SSH[@]}" "mkdir -p ~/c4-unit-switch/$sha && tar xf - -C ~/c4-unit-switch/$sha"
  exec "${SSH[@]}" "sudo -n bash ~/c4-unit-switch/$sha/pi/deploy/switch-units-box.sh --rollback \"\$HOME/deploy-backups/$2\" ${3:-}"
fi

box=${1:-}
[[ $box == orin || $box == pi ]] || { echo "box must be orin or pi" >&2; exit 2; }
apply=${2:-}
[[ -z $apply || $apply == --apply ]] || { echo "unknown option: $apply" >&2; exit 2; }
git diff --quiet HEAD -- pi/systemd pi/deploy/switch-units-box.sh \
  || { echo "pi/systemd or the switch script has uncommitted changes; commit or stash first." >&2; exit 2; }

stamp=$(date -u +%Y%m%dT%H%M%SZ)
git archive HEAD pi/deploy/switch-units-box.sh pi/systemd \
  | "${SSH[@]}" "rm -rf ~/c4-unit-switch/$sha && mkdir -p ~/c4-unit-switch/$sha && tar xf - -C ~/c4-unit-switch/$sha"
echo "== $HOST ($box), units from $sha"
"${SSH[@]}" "sudo -n bash ~/c4-unit-switch/$sha/pi/deploy/switch-units-box.sh $box \"\$HOME/c4-unit-switch/$sha\" \"\$HOME/deploy-backups/units-$stamp\" $apply"
