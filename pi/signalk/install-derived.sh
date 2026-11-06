#!/bin/sh
# Install, enable and patch the signalk-derived-data plugin in the Signal K config volume.
# Run by the `signalk-derived` service in compose.pi.yml, before Signal K starts.
#
# Rules (#171, settled in #240 / #254):
#   - The plugin is pinned to exactly $PIN. A different or missing install is replaced with an
#     exact install; a matching one is left alone (no network needed).
#   - Raw Signal K must start even when this fails. So this script NEVER exits non-zero: it
#     logs "[derived] FAILED ..." and leaves the affected channels missing instead.
#   - A failed derivation turns off only its own channels:
#       plugin not installed  -> no derived channels (true wind, VMG); raw data unaffected
#       ground-wind patch fails -> groundWind calc turned off (directionTrueGround and
#                                  angleTrueGround missing); water true wind + VMG stay on
#
# Env overrides exist for the test (pi/signalk/test_install_derived.py).
PIN="1.45.0"
SK="${SK_CONFIG:-/home/node/.signalk}"
SRC="${SRC_DIR:-/src}"
NPM_TIMEOUT_S="${NPM_TIMEOUT_S:-120}"  # offline npm retries for minutes; cap it
PLUGIN="$SK/node_modules/signalk-derived-data"
CONF_DIR="$SK/plugin-config-data"
CONF="$CONF_DIR/derived-data.json"
# Present while this script (not the crew) has turned groundWind off after a failed patch.
MARK="$CONF_DIR/.groundwind-off-patch-failed"

installed_version() {
  [ -f "$PLUGIN/package.json" ] || return 0
  sed -n 's/^  "version": *"\([^"]*\)".*/\1/p' "$PLUGIN/package.json" | head -n 1
}

cd "$SK" || { echo "[derived] FAILED: no config dir $SK. Signal K starts without derived data." >&2; exit 0; }

have="$(installed_version)"
if [ "$have" != "$PIN" ]; then
  echo "[derived] signalk-derived-data is '${have:-not installed}', pinned $PIN: installing $PIN..."
  if ! timeout "$NPM_TIMEOUT_S" npm install --prefix "$SK" --save-exact --no-audit --no-fund \
      "signalk-derived-data@$PIN"; then
    echo "[derived] FAILED: npm install signalk-derived-data@$PIN (offline?)" >&2
  fi
  have="$(installed_version)"
else
  echo "[derived] signalk-derived-data $PIN already installed"
fi

if [ -z "$have" ]; then
  echo "[derived] FAILED: plugin not installed. Signal K starts without true wind and VMG;" \
       "those channels stay missing." >&2
  exit 0
fi
if [ "$have" != "$PIN" ]; then
  echo "[derived] FAILED: plugin is $have, not the pinned $PIN. Running it anyway." >&2
fi

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF" ]; then
  if cp "$SRC/derived-data.json" "$CONF"; then
    echo "[derived] enabled derived-data plugin config:"
    cat "$CONF"
  else
    echo "[derived] FAILED: could not write $CONF. The plugin stays off." >&2
    exit 0
  fi
else
  echo "[derived] derived-data.json already present (leaving as-is)"
fi

# Split the two true-wind directions onto separate paths (see the patch script). Runs on every
# boot so a reinstall comes back patched.
if sh "$SRC/patch-windground-path.sh" "$PLUGIN"; then
  if [ -f "$MARK" ]; then
    sed -i 's/"groundWind": *false/"groundWind": true/' "$CONF" && rm -f "$MARK"
    echo "[derived] patch applies again: ground wind turned back on"
  fi
else
  # Unpatched, groundWind would publish ground-referenced direction on directionTrue, mixed with
  # the water-referenced one. Turn just that calc off.
  if grep -q '"groundWind": *true' "$CONF"; then
    sed -i 's/"groundWind": *true/"groundWind": false/' "$CONF" && touch "$MARK"
  fi
  if grep -q '"groundWind": *false' "$CONF"; then
    echo "[derived] FAILED: ground-wind patch. Ground wind turned off (directionTrueGround," \
         "angleTrueGround missing); water true wind and VMG stay on." >&2
  else
    echo "[derived] FAILED: ground-wind patch, and groundWind could not be turned off in $CONF." \
         "directionTrue may mix ground and water wind." >&2
  fi
fi
exit 0
