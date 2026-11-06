#!/usr/bin/env bash
# Routing baselines (#255): run tools/analysis/route_baselines.py in the CI lab image, offline.
# Usage: tools/analysis/route_baselines.sh <cache-dir> <out-dir> [args to route_baselines.py...]
#   <cache-dir> holds copies of the Lab's shoreline caches: coastline/ (from the lab_coastline
#   volume) and enc/ (from lab_enc). Copies, because a new bbox writes a clip into them.
#   Pass --boat /cache/<file>.json to route on another boat profile (e.g. the live Lab's, with its
#   helm factor and polar cells); the default is the repo's vps/lab/boats/sr33.json.
# Build the image first if needed: tools/ci/run.sh route_ (or `make ci`) builds sr33-ci-lab.
set -euo pipefail
cache=$(realpath "$1") out=$(realpath "$2"); shift 2
cd "$(git rev-parse --show-toplevel)"
docker run --rm --network none -v "$PWD:/repo:ro" -v "$cache:/cache" -v "$out:/out" -w /srv \
  -e PYTHONPATH=/repo/vps/lab:/repo -e PYTHONDONTWRITEBYTECODE=1 \
  -e COASTLINE_CACHE=/cache/coastline -e ENC_CACHE=/cache/enc -e COASTLINE_GLOBAL=gshhg -e GSHHG_RES=f \
  sr33-ci-lab python3 /repo/tools/analysis/route_baselines.py --out /out/route_baselines.json "$@"
