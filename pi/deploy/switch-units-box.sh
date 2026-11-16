#!/usr/bin/env bash
# Runs ON a box, as root: switch its systemd units from the old sr33-* names to c4-* (#314 PR D).
# Called by pi/deploy/switch-units.sh over ssh; not meant to be run by hand.
#
#   switch-units-box.sh orin|pi SRC BACKUP [--apply]      # dry run unless --apply
#   switch-units-box.sh --rollback BACKUP [--apply]
#
# SRC holds pi/systemd from the checkout. BACKUP is where the old unit files and the plan go
# (~/deploy-backups/units-STAMP); a rollback reads it back. What it does with --apply:
#   * copies /etc/sr33/* to /etc/c4/ (never overwrites; /etc/sr33 stays as it was)
#   * moves REROUTE_BOAT from the old re-route unit into /etc/c4/reroute.env
#   * installs the c4-* units, carries over each old unit's enabled/running state, removes the old
#   * the Pi's can0 unit is only re-enabled, never stopped or started: stopping it takes the
#     NMEA 2000 bus down, and starting it while can0 is up fails. The new name runs from next boot.
# ROOT (tests only) prefixes every system path.
set -euo pipefail

R=${ROOT:-}
UNITS=$R/etc/systemd/system
OLD_ENV=$R/etc/sr33
NEW_ENV=$R/etc/c4
SHED=$R/run/c4-shed/state.json
WAIT_S=${WAIT_S:-30}

# old name -> new name, in the order they switch: the load-shed guard first, so the copilot,
# re-route and plume come up reading its new state file; the plume's timer before its service.
ORIN=(sr33-orin-shed.service sr33-orin-copilot.service sr33-orin-copilot-dashboard.service
      sr33-orin-reroute.service sr33-orin-plume.timer sr33-orin-plume.service)
PI=(sr33-can0.service)
DROPIN_OLD=ollama.service.d/sr33-oom.conf
DROPIN_NEW=ollama.service.d/c4-oom.conf

new_name() { echo "c4-${1#sr33-}"; }
say() { echo "  $*"; }
die() { echo "STOPPED: $*" >&2; exit 1; }
state() { systemctl "$1" "$2" 2>/dev/null || true; }

# a unit switched over by name only: no stop, no start (see the top)
no_restart() { [[ $1 == sr33-can0.service ]]; }

wait_active() {   # unit -> 0 once active, 1 after WAIT_S
  local i
  for ((i = 0; i <= WAIT_S; i++)); do
    [[ $(state is-active "$1") == active ]] && return 0
    sleep 1
  done
  return 1
}

# The REROUTE_BOAT to add to /etc/c4/reroute.env, or nothing. Nothing when the file /etc/c4 will
# have (its own, or the copy of /etc/sr33's) already sets one. Otherwise the old env file's value,
# else the one the old re-route unit set inline.
boat_to_move() {
  local env=$NEW_ENV/reroute.env v
  [[ -f $env ]] || env=$OLD_ENV/reroute.env
  if grep -qs '^[[:space:]]*REROUTE_BOAT=.' "$env"; then return; fi
  v=$(sed -n 's/^[[:space:]]*REROUTE_BOAT=\(..*\)$/\1/p' "$OLD_ENV/reroute.env" 2>/dev/null | tail -1)
  if [[ -z $v && -f $UNITS/sr33-orin-reroute.service ]]; then   # (a unit with overrides is refused)
    v=$(sed -n 's/^[[:space:]]*Environment=//p' "$UNITS/sr33-orin-reroute.service" \
        | tr ' ' '\n' | tr -d "\"'" \
        | sed -n 's/^REROUTE_BOAT=\(..*\)$/\1/p' | tail -1)   # Environment=A=1 "REROUTE_BOAT=x" too
  fi
  echo "$v"
}

# ---------------------------------------------------------------------------------------------
rollback() {
  local bk=$1 apply=$2 old new en act
  [[ -f $bk/plan ]] || die "no plan in $bk. Nothing done."
  echo "Rollback from $bk:"
  while read -r old en act; do
    new=$(new_name "$old")
    say "$new -> $old (was ${en}, ${act})"
  done < "$bk/plan"
  if [[ -f $bk/$(basename "$DROPIN_OLD") ]]; then say "ollama drop-in: c4-oom.conf -> sr33-oom.conf"; fi
  if [[ $apply != 1 ]]; then echo "Dry run. Add --apply to roll back."; return; fi

  while read -r old en act; do
    cp -p "$bk/$old" "$UNITS/$old"
  done < "$bk/plan"
  if [[ -f $bk/$(basename "$DROPIN_OLD") ]]; then
    cp -p "$bk/$(basename "$DROPIN_OLD")" "$UNITS/$DROPIN_OLD"
    rm -f "$UNITS/$DROPIN_NEW"
  fi
  systemctl daemon-reload
  while read -r old en act; do
    new=$(new_name "$old")
    if no_restart "$old"; then
      systemctl disable "$new" || true
    else
      systemctl disable --now "$new" || true
    fi
    if [[ $old == *.timer ]]; then
      systemctl stop "$(new_name "${old%.timer}.service")" || true   # no pass beside the old timer's
    fi
    if [[ $en == enabled ]]; then systemctl enable "$old"; fi
    if [[ $act == active ]] && ! no_restart "$old"; then systemctl start "$old"; fi
    rm -f "$UNITS/$new"
  done < "$bk/plan"
  systemctl daemon-reload
  echo "Rolled back. /etc/c4 is left in place (the old units read /etc/sr33)."
  echo "On the Orin, also put the checkout back (~/pre-pull-backup): the old shed unit writes"
  echo "/run/sr33-shed and newer code reads /run/c4-shed, so the shed would be ignored."
}

# ---------------------------------------------------------------------------------------------
switch() {
  local box=$1 src=$2 bk=$3 apply=$4 list old new en act boat line
  case $box in
    orin) list=("${ORIN[@]}") ;;
    pi) list=("${PI[@]}") ;;
    *) die "box must be orin or pi, not '$box'. Nothing done." ;;
  esac

  echo "Units on this box:"
  local todo=()
  for old in "${list[@]}"; do
    new=$(new_name "$old")
    [[ -f $src/pi/systemd/$new ]] || die "the checkout has no pi/systemd/$new. Nothing done."
    if [[ -f $UNITS/$old && -e $UNITS/$new ]]; then
      die "both $old and $new are installed; remove one by hand first. Nothing done."
    elif [[ -d $UNITS/$old.d || -d $UNITS/$new.d ]]; then
      # a box's own overrides can name old paths or clash with the new unit: a person moves them
      die "$old has overrides ($old.d or $new.d); move them by hand first. Nothing done."
    elif [[ -f $UNITS/$old ]]; then
      en=$(state is-enabled "$old"); act=$(state is-active "$old")
      say "$old (${en:-?}, ${act:-?}) -> $new"
      todo+=("$old ${en:-unknown} ${act:-unknown}")
    elif [[ -f $UNITS/$new ]]; then
      say "$new already in place"
    else
      say "$old not installed, skipped"
    fi
  done
  if [[ -f $UNITS/$DROPIN_OLD ]]; then say "ollama drop-in: sr33-oom.conf -> c4-oom.conf (same setting)"; fi
  if [[ -d $OLD_ENV ]]; then
    for f in "$OLD_ENV"/*; do
      [[ -e $f ]] || continue
      if [[ -e $NEW_ENV/$(basename "$f") ]]; then say "/etc/c4/$(basename "$f") already there, kept"
      else say "/etc/sr33/$(basename "$f") -> /etc/c4/ (copied; the old one stays)"; fi
    done
  fi
  boat=$(boat_to_move)
  if [[ -n $boat ]]; then say "REROUTE_BOAT=$boat goes into /etc/c4/reroute.env"; fi
  if [[ $box == pi && ${#todo[@]} -gt 0 ]]; then
    say "can0 is not stopped or started; c4-can0 takes over at the next boot"
  fi
  if [[ ${#todo[@]} -eq 0 ]]; then echo "Nothing to switch."; return; fi
  if [[ $apply != 1 ]]; then echo "Dry run. Add --apply to switch."; return; fi

  # 1. back up the old files and write the plan a rollback reads
  mkdir -p "$bk"
  : > "$bk/plan"
  for line in "${todo[@]}"; do
    read -r old en act <<< "$line"
    cp -p "$UNITS/$old" "$bk/$old"
    echo "$old $en $act" >> "$bk/plan"
  done
  if [[ -f $UNITS/$DROPIN_OLD ]]; then cp -p "$UNITS/$DROPIN_OLD" "$bk/"; fi
  if [[ -d $OLD_ENV ]]; then cp -rp "$OLD_ENV" "$bk/etc-old"; fi
  echo "Backed up to $bk"

  # 2. config: /etc/sr33 -> /etc/c4, never overwriting
  if [[ -d $OLD_ENV ]]; then
    mkdir -p "$NEW_ENV"
    for f in "$OLD_ENV"/*; do
      if [[ -e $f && ! -e $NEW_ENV/$(basename "$f") ]]; then cp -p "$f" "$NEW_ENV/"; fi
    done
  fi
  if [[ -n $boat ]]; then
    mkdir -p "$NEW_ENV"
    [[ -f $NEW_ENV/reroute.env ]] || install -m 0644 /dev/null "$NEW_ENV/reroute.env"
    printf '# the boat this Orin re-routes (moved from the old setup, #314 PR D)\nREROUTE_BOAT=%s\n' \
      "$boat" >> "$NEW_ENV/reroute.env"
  fi

  # 3. install the new units next to the old ones
  for line in "${todo[@]}"; do
    read -r old en act <<< "$line"
    new=$(new_name "$old")
    install -m 0644 "$src/pi/systemd/$new" "$UNITS/$new"
  done
  if [[ -f $UNITS/$DROPIN_OLD ]]; then
    install -m 0644 "$src/pi/systemd/$DROPIN_NEW" "$UNITS/$DROPIN_NEW"
    rm -f "$UNITS/$DROPIN_OLD"
  fi
  systemctl daemon-reload

  # 4. hand over, one unit at a time
  local failed=()
  for line in "${todo[@]}"; do
    read -r old en act <<< "$line"
    new=$(new_name "$old")
    if [[ $en == enabled ]]; then
      systemctl disable "$old"
      systemctl enable "$new"
    fi
    if no_restart "$old"; then
      say "$new enabled (runs from next boot)"
    elif [[ $act == active || $act == activating ]]; then
      systemctl stop "$old"
      if [[ $old == *.timer ]]; then
        # the new timer may fire at once: let no pass of the old service run beside it
        systemctl stop "${old%.timer}.service" || true
      fi
      if [[ $old == *plume.service ]]; then
        say "$new is run by its timer"      # a oneshot pass; the timer starts the next one
      else
        if systemctl start "$new" && wait_active "$new"; then say "$new running"; else failed+=("$new"); fi
      fi
    else
      say "$new installed (was ${act}, left so)"
    fi
  done

  # 5. remove the old unit files (backed up above)
  for line in "${todo[@]}"; do
    read -r old en act <<< "$line"
    rm -f "$UNITS/$old"
  done
  systemctl daemon-reload

  # 6. check
  for line in "${todo[@]}"; do
    read -r old en act <<< "$line"
    new=$(new_name "$old")
    if [[ $en == enabled && $(state is-enabled "$new") != enabled ]]; then failed+=("$new not enabled"); fi
  done
  if grep -q '^sr33-orin-shed.service .* active$' "$bk/plan"; then
    local i
    for ((i = 0; i <= WAIT_S; i++)); do [[ -f $SHED ]] && break; sleep 1; done
    if [[ ! -f $SHED ]]; then failed+=("no shed state at /run/c4-shed/state.json"); fi
  fi
  if [[ ${#failed[@]} -gt 0 ]]; then
    echo "PROBLEM: ${failed[*]}" >&2
    echo "Roll back with: switch-units.sh <host> --rollback $(basename "$bk") --apply" >&2
    exit 1
  fi
  echo "Switched. Backup: $bk"
}

# ---------------------------------------------------------------------------------------------
if [[ ${1:-} == --rollback ]]; then
  [[ $# -ge 2 ]] || die "usage: --rollback BACKUP [--apply]"
  rollback "$2" "$([[ ${3:-} == --apply ]] && echo 1 || echo 0)"
else
  [[ $# -ge 3 ]] || die "usage: orin|pi SRC BACKUP [--apply]"
  switch "$1" "$2" "$3" "$([[ ${4:-} == --apply ]] && echo 1 || echo 0)"
fi
