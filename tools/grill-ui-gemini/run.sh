#!/usr/bin/env bash
# One command: screenshot the Lab + the race console at iPad sizes, have Gemini grill them.
#   tools/grill-ui-gemini/run.sh --model cheap --only lab-polar,console --max-usd 0.5
#   tools/grill-ui-gemini/run.sh --dry-run          (no key, no cost)
#   tools/grill-ui-gemini/run.sh --help             (all options)
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -d "$here/node_modules/@google/genai" ] || [ ! -d "$here/node_modules/playwright" ]; then
  (cd "$here" && npm ci --no-audit --no-fund >/dev/null)
fi
exec node "$here/src/cli.mjs" "$@"
