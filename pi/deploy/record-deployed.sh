#!/usr/bin/env bash
# Record on a box exactly what a deploy put there (#187): the hash of every copied file as it sits
# on the box now, and for each rebuilt service the image it was built as and the image its
# container runs. Without this, "what's on the boat" takes forensics: the Pi's clone sits on an
# old commit with copied files on top, so its git can't say.
#
# Run from the checkout you deployed from, right after the copy + rebuild:
#
#   pi/deploy/record-deployed.sh HOST STAMP LABEL [FILE ...] [-- SERVICE ...]
#   pi/deploy/record-deployed.sh "$PI_HOST" 20261106T204014Z w45 pi/engine/engine_app.py -- engine
#
# On the box it writes ~/deploy-backups/LABEL-STAMP/DEPLOYED and appends the same block to
# ~/DEPLOYED (one block per deploy, newest last). Here it checks every file hash against this
# checkout and exits 1 if one differs, or if a container doesn't run the image just built.
#
# Env: REMOTE_REPO (default Agent_C4, under the box user's home), BACKUPS (default
# deploy-backups), COMPOSE_PROJECT (default sr33-pi: images <project>-<svc>, containers
# <project>-<svc>-1). Services are optional: the Orin runs systemd units, not images.
set -euo pipefail

if [[ ${1:-} == --box ]]; then
  # ---- runs ON the box (fed over ssh): STAMP LABEL COMMIT REPO BACKUPS PROJECT FILE... -- SVC...
  shift
  stamp=$1 label=$2 commit=$3 repo=$4 backups=$5 project=$6; shift 6
  files=() services=()
  while [[ $# -gt 0 && $1 != -- ]]; do files+=("$1"); shift; done
  [[ ${1:-} == -- ]] && shift
  services=("$@")
  cd ~/"$repo"
  dir=~/"$backups/$label-$stamp"
  mkdir -p "$dir"
  clone="?" local_changes="?"
  if git rev-parse --git-dir >/dev/null 2>&1; then
    clone=$(git rev-parse --short HEAD) local_changes=$(git status --porcelain | wc -l)
  fi
  {
    echo "deploy $stamp $label from $commit"
    echo "recorded $(date -u +%FT%TZ) on $(hostname) in ~/$repo (clone at $clone, $local_changes local changes)"
    for f in "${files[@]}"; do
      if [[ -f $f ]]; then echo "file $(sha256sum "$f" | cut -d' ' -f1) $f"
      else echo "file MISSING $f"; fi
    done
    for s in "${services[@]}"; do
      built=$(docker image inspect -f '{{.Id}}' "$project-$s" 2>/dev/null || echo "?")
      running=$(docker inspect -f '{{.Image}}' "$project-$s-1" 2>/dev/null || echo "?")
      echo "image $s built $built running $running"
    done
  } > "$dir/DEPLOYED.tmp"
  mv "$dir/DEPLOYED.tmp" "$dir/DEPLOYED"
  { cat "$dir/DEPLOYED"; echo; } >> ~/DEPLOYED
  cat "$dir/DEPLOYED"
  exit 0
fi

# ---- runs HERE, on the machine you deployed from
host=${1:?usage: record-deployed.sh HOST STAMP LABEL [FILE ...] [-- SERVICE ...]}
stamp=${2:?STAMP missing} label=${3:?LABEL missing}; shift 3
[[ $stamp =~ ^[A-Za-z0-9._-]+$ && $label =~ ^[A-Za-z0-9._-]+$ ]] \
  || { echo "STAMP and LABEL may only use letters, digits, . _ -" >&2; exit 2; }
files=() services=()
while [[ $# -gt 0 && $1 != -- ]]; do files+=("$1"); shift; done
[[ ${1:-} == -- ]] && shift
services=("$@")
for x in "${files[@]}" "${services[@]}"; do
  [[ $x =~ ^[A-Za-z0-9._/@+-]+$ ]] || { echo "unsafe name (spaces or shell characters): $x" >&2; exit 2; }
done

script=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")   # before the cd below changes "."
cd "$(git rev-parse --show-toplevel)"
commit=$(git rev-parse --short HEAD)
if [[ ${#files[@]} -gt 0 && -n $(git status --porcelain -- "${files[@]}") ]]; then commit+="+uncommitted"; fi

out=$(ssh -o ConnectTimeout=15 "$host" bash -s -- --box "$stamp" "$label" "$commit" \
        "${REMOTE_REPO:-Agent_C4}" "${BACKUPS:-deploy-backups}" "${COMPOSE_PROJECT:-sr33-pi}" \
        "${files[@]}" -- "${services[@]}" < "$script") \
  || { echo "!! could not record the deploy on $host" >&2; exit 1; }
echo "$out"

bad=0
declare -A seen=()
while read -r kind a b c d e; do
  case $kind in
    file)
      seen[$b]=1
      if [[ $a == MISSING ]]; then echo "!! $b is not on the box"; bad=1
      elif [[ $a != "$(sha256sum "$b" | cut -d' ' -f1)" ]]; then echo "!! $b on the box differs from this checkout"; bad=1; fi ;;
    image)
      # image <svc> built <id> running <id>
      if [[ $c == "?" || $c != "$e" ]]; then echo "!! $a: the container does not run the image just built"; bad=1; fi ;;
  esac
done <<<"$out"
for f in "${files[@]}"; do [[ -n ${seen[$f]:-} ]] || { echo "!! no hash came back for $f"; bad=1; }; done
[[ $bad -eq 0 ]] && echo "recorded: ${#files[@]} file(s) match this checkout, ${#services[@]} service image(s) running as built"
exit $bad
