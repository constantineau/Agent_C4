#!/bin/sh
# Give ground-referenced wind direction its own Signal K path.
#
# signalk-derived-data ships two calcs that both publish `environment.wind.directionTrue`:
#   - calcs/windDirection.js  (optionKey `directionTrue`) — WATER-referenced,
#                             headingTrue + angleTrueWater
#   - calcs/windGround.js     (optionKey `groundWind`)    — GROUND-referenced,
#                             headingTrue + atan2 over SOG-corrected apparent wind
# derived-data.json enables both, so two different quantities land on one path under one
# `$source`, differing by current + leeway and indistinguishable downstream. In the Jul 18
# race archive that produced 9,595 duplicate (time, source, path) rows; in live data the
# uplink's per-window "latest value wins" collapse hid it by arbitrarily publishing one or
# the other, making `directionTrue` a nondeterministic mix rather than a stable quantity.
#
# This repoints ONLY the groundWind calc onto `environment.wind.directionTrueGround`,
# pairing with the `angleTrueGround` it already emits, and leaves `directionTrue` to the
# water-referenced calc that polar coaching and tactics actually want.
#
# Idempotent: safe to re-run, and re-runs on every `signalk-derived` boot so a plugin
# reinstall into a fresh sk_config volume gets patched too.
set -e

PLUGIN_DIR="${1:-/home/node/.signalk/node_modules/signalk-derived-data}"
F="$PLUGIN_DIR/dist/calcs/windGround.js"
NEW_PATH="environment.wind.directionTrueGround"

if [ ! -f "$F" ]; then
  echo "[patch] $F not found — plugin not installed yet, nothing to do"
  exit 0
fi

if grep -q "$NEW_PATH" "$F"; then
  echo "[patch] windGround.js already publishes $NEW_PATH — leaving as-is"
  exit 0
fi

[ -f "$F.orig" ] || cp "$F" "$F.orig"

# The trailing quote in the pattern keeps this from matching an already-patched
# `...directionTrueGround'`, so the guard above is belt-and-braces rather than load-bearing.
sed -i "s/'environment\.wind\.directionTrue'/'$NEW_PATH'/g" "$F"

if grep -q "'environment\.wind\.directionTrue'" "$F"; then
  echo "[patch] FAILED — windGround.js still references environment.wind.directionTrue" >&2
  cp "$F.orig" "$F"
  exit 1
fi

echo "[patch] windGround.js now publishes $NEW_PATH (original kept at windGround.js.orig)"
