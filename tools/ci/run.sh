#!/usr/bin/env bash
# Every test suite in the repo, each in a throwaway container off its own service's image, built
# fresh from this tree (tagged sr33-ci-*, never the running stacks' images). `make ci` runs this.
# Usage: tools/ci/run.sh [filter]   — only test files whose path contains <filter>.
#
# Discovery is automatic: each area below is a glob, so a new test_* file in a known area runs
# without touching this script. A test file in a NEW place fails the run ("not wired") until it
# gets an area line here, or a reasoned entry in EXCLUDED.
#
# A file fails when it exits non-zero, or when it exits 0 but printed a "[FAIL]" check line.
# Tests report skips as "[SKIP] reason" and accepted known failures as "[KNOWN-FAIL] reason (#issue)";
# both are listed in the summary at the end, so nothing is skipped silently.
set -uo pipefail
shopt -s globstar nullglob
cd "$(git rev-parse --show-toplevel)"
repo=$PWD filter=${1:-}
fails=() passes=0 skips=() known=()
declare -A wired=()

# Test files deliberately not run in CI, with the reason.
declare -A EXCLUDED=(
  # needs `npm install` (playwright + @google/genai) and network; it is dev tooling, not boat code.
  [tools/grill-ui-gemini/test/grill.test.mjs]="grill tooling: needs npm deps (playwright) + network"
)

# build <svc> <dockerfile>: the service image, plus pytest/freezegun on top for the test layer.
build() {
  docker build -q -t "sr33-ci-$1-base" -f "$2" . >/dev/null || { echo "BUILD FAILED: $1"; exit 1; }
  printf 'FROM sr33-ci-%s-base\nRUN pip install --no-cache-dir -q pytest freezegun httpx\n' "$1" \
    | docker build -q -t "sr33-ci-$1" - >/dev/null || { echo "BUILD FAILED: $1 (test layer)"; exit 1; }
}

# run <svc> <workdir> <pythonpath> <file> [cmd...]: one test file; default cmd runs it as a script.
run() {
  local svc=$1 wd=$2 pp=$3 f=$4; shift 4
  wired[$f]=1
  [[ -n $filter && $f != *"$filter"* ]] && return
  local cmd=("$@"); [[ ${#cmd[@]} -eq 0 ]] && cmd=(python3 "/repo/$f")
  local log rc; log=$(docker run --rm --network none -v "$repo:/repo:ro" -w "$wd" \
    -e PYTHONPATH="$pp" -e PYTHONDONTWRITEBYTECODE=1 -e C4_CI=1 "sr33-ci-$svc" "${cmd[@]}" 2>&1)
  rc=$?
  local line
  while IFS= read -r line; do skips+=("$f: ${line#*] }"); done \
    < <(grep -E '\[ *(SKIP|skip) *\]' <<<"$log")
  while IFS= read -r line; do known+=("$f: ${line#*] }"); done \
    < <(grep -E '\[KNOWN-FAIL\]' <<<"$log")
  if [[ $rc -eq 0 ]] && grep -qE '\[ *FAIL *\]' <<<"$log"; then
    rc=99; log+=$'\n'"(exited 0 but printed a [FAIL] check: the file does not exit non-zero on failure)"
  fi
  if [[ $rc -eq 0 ]]; then passes=$((passes + 1)); echo "ok    $f"
  else fails+=("$f"); echo "FAIL  $f"; echo "$log" | tail -25 | sed 's/^/      /'; fi
}

# area <svc> <workdir> <pythonpath> <glob...>: every python test file the globs match. A file that
# imports pytest runs under pytest; the rest are scripts that exit non-zero on failure.
area() {
  local svc=$1 wd=$2 pp=$3 f; shift 3
  for f in "$@"; do
    if grep -qE '^(import|from) pytest' "$f"; then
      run "$svc" "$wd" "$pp" "$f" python3 -m pytest -q -p no:cacheprovider "/repo/$f"
    else run "$svc" "$wd" "$pp" "$f"; fi
  done
}

# orin_area <svc> <glob...>: the Orin copilot is a package (relative imports): run as a module.
orin_area() {
  local svc=$1 f m; shift
  for f in "$@"; do
    m=${f#pi/orin/}; m=${m%.py}; m=${m//\//.}
    run "$svc" /repo/pi/orin /repo/pi/orin:/repo/vps/agent:/repo "$f" python3 -m "$m"
  done
}

# node_area <glob...>: plain node scripts (no build step, no npm deps); *.test.mjs via node --test.
node_area() {
  local f
  for f in "$@"; do
    [[ -n ${EXCLUDED[$f]:-} ]] && continue
    if [[ $f == *.test.mjs || $f == *.test.js ]]; then run node /repo /repo "$f" node --test "/repo/$f"
    else run node /repo /repo "$f" node "/repo/$f"; fi
  done
}

build archiver pi/archiver/Dockerfile
build uplink pi/uplink/Dockerfile
build n2kout pi/n2kout/Dockerfile
build agent vps/agent/Dockerfile
build lab vps/lab/Dockerfile
docker image inspect node:22-alpine >/dev/null 2>&1 || docker pull -q node:22-alpine >/dev/null \
  || { echo "PULL FAILED: node:22-alpine"; exit 1; }
docker tag node:22-alpine sr33-ci-node

area archiver /repo /repo/pi/archiver:/repo pi/archiver/**/test_*.py
area uplink /repo /repo pi/uplink/**/test_*.py
area n2kout /repo /repo/pi/n2kout pi/n2kout/**/test_*.py
orin_area agent pi/orin/copilot/**/test_*.py
area agent /repo /repo/vps/agent:/repo:/repo/pi/archiver vps/agent/**/test_*.py
# shared/ is pure stdlib, imported by every service; the agent image is as good as any.
area agent /repo /repo shared/**/test_*.py
# The lab reads its data from the image's /srv layout (races, boats, polars), built from this tree.
area lab /srv /srv vps/lab/**/test_*.py
# The Orin's onboard plume and re-route reuse the Lab's wind code and optimizer (#124), so they
# test in the Lab image (eccodes/cfgrib).
area lab /srv /srv pi/orin/plume/**/test_*.py pi/orin/reroute/**/test_*.py
# The console's pure JS modules and the replay rig: plain node, pinned major.
node_area pi/console/**/test_*.js tools/replay/**/test_*.js tools/**/*.test.mjs

# Every tracked test file must have run (or be excluded with a reason): nothing is missed silently.
unwired=()
while IFS= read -r f; do
  [[ -n ${wired[$f]:-} || -n ${EXCLUDED[$f]:-} ]] || unwired+=("$f")
done < <(git ls-files | grep -E '(^|/)test_[^/]*\.(py|js|mjs)$|\.test\.(js|mjs)$')
for f in "${unwired[@]}"; do fails+=("$f (not wired into tools/ci/run.sh)"); done

echo
for f in "${!EXCLUDED[@]}"; do echo "excluded  $f: ${EXCLUDED[$f]}"; done
echo "${#skips[@]} skipped checks"
for s in "${skips[@]}"; do echo "  skip  $s"; done
echo "${#known[@]} known failures"
for s in "${known[@]}"; do echo "  known $s"; done
echo; echo "$passes passed, ${#fails[@]} failed"
[[ ${#fails[@]} -eq 0 ]] || { printf '  %s\n' "${fails[@]}"; exit 1; }
