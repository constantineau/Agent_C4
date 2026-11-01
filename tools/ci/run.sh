#!/usr/bin/env bash
# Every test suite in the repo, each in a throwaway container off its own service's image, built
# fresh from this tree (tagged sr33-ci-*, never the running stacks' images). `make ci` runs this.
# Usage: tools/ci/run.sh [filter]   — only test files whose path contains <filter>.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
repo=$PWD filter=${1:-}
fails=() passes=0

# build <svc> <dockerfile>: the service image, plus pytest/freezegun on top for the test layer.
build() {
  docker build -q -t "sr33-ci-$1-base" -f "$2" . >/dev/null || { echo "BUILD FAILED: $1"; exit 1; }
  printf 'FROM sr33-ci-%s-base\nRUN pip install --no-cache-dir -q pytest freezegun httpx\n' "$1" \
    | docker build -q -t "sr33-ci-$1" - >/dev/null || { echo "BUILD FAILED: $1 (test layer)"; exit 1; }
}

# run <svc> <workdir> <pythonpath> <file> [cmd...]: one test file; default cmd runs it as a script.
run() {
  local svc=$1 wd=$2 pp=$3 f=$4; shift 4
  [[ -n $filter && $f != *"$filter"* ]] && return
  local cmd=("$@"); [[ ${#cmd[@]} -eq 0 ]] && cmd=(python3 "/repo/$f")
  local log; log=$(docker run --rm --network none -v "$repo:/repo:ro" -w "$wd" \
    -e PYTHONPATH="$pp" -e PYTHONDONTWRITEBYTECODE=1 "sr33-ci-$svc" "${cmd[@]}" 2>&1)
  if [[ $? -eq 0 ]]; then passes=$((passes + 1)); echo "ok    $f"
  else fails+=("$f"); echo "FAIL  $f"; echo "$log" | tail -25 | sed 's/^/      /'; fi
}

build archiver pi/archiver/Dockerfile
build uplink pi/uplink/Dockerfile
build n2kout pi/n2kout/Dockerfile
build agent vps/agent/Dockerfile
build lab vps/lab/Dockerfile

for f in pi/archiver/test_*.py; do run archiver /repo /repo/pi/archiver:/repo "$f"; done
run uplink /repo /repo "pi/uplink/test_uplink_queue.py" python3 -m pytest -q -p no:cacheprovider /repo/pi/uplink/test_uplink_queue.py
run n2kout /repo /repo/pi/n2kout "pi/n2kout/test_n2k.py"
run agent /repo/pi/orin /repo/pi/orin:/repo/vps/agent:/repo "pi/orin/copilot/eval/test_eval.py" python3 -m copilot.eval.test_eval
run agent /repo/pi/orin /repo/pi/orin:/repo/vps/agent:/repo "pi/orin/copilot/test_collision_watch.py" python3 -m copilot.test_collision_watch
for f in vps/agent/test_*.py; do run agent /repo /repo/vps/agent:/repo:/repo/pi/archiver "$f"; done
# The console's pure JS modules (no build step, no npm deps): plain node, pinned major.
docker image inspect node:22-alpine >/dev/null 2>&1 || docker pull -q node:22-alpine >/dev/null \
  || { echo "PULL FAILED: node:22-alpine"; exit 1; }
docker tag node:22-alpine sr33-ci-node
run node /repo /repo "pi/console/test_crewwatch.js" node /repo/pi/console/test_crewwatch.js
run node /repo /repo "pi/console/test_plume.js" node /repo/pi/console/test_plume.js
run node /repo /repo "tools/replay/test_overrides.js" node /repo/tools/replay/test_overrides.js
# The lab reads its data from the image's /srv layout (races, boats, polars), built from this tree.
for f in vps/lab/test_*.py; do run lab /srv /srv "$f"; done
# The Orin's onboard plume reuses the Lab's wind code, so it tests in the Lab image (eccodes/cfgrib).
run lab /srv /srv "pi/orin/plume/test_onboard_plume.py"

echo; echo "$passes passed, ${#fails[@]} failed"
[[ ${#fails[@]} -eq 0 ]] || { printf '  %s\n' "${fails[@]}"; exit 1; }
