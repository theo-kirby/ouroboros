#!/bin/sh
# Copy Governor's report embed (the hub graph renderer, one IIFE plus its CSS) into
# the package, for the "Memory graph" section of a run's page. Run after a change
# to Governor's src/report.tsx, src/HubCanvas.tsx or src/hub-model.ts.
#   scripts/vendor-governor-report.sh [path to the governor checkout, default ../governor]
set -eu
GOV="${1:-$(dirname "$0")/../../governor}"
OUT="$(dirname "$0")/../src/ouroboros/vendor"
(cd "$GOV" && npm run --silent build:report >/dev/null)
cp "$GOV/dist-report/embed.js" "$OUT/governor-report.js"
cp "$GOV/dist-report/embed.css" "$OUT/governor-report.css"
printf '%s\n' "$(cd "$GOV" && git rev-parse --short HEAD)$(cd "$GOV" && git diff --quiet HEAD -- src shared || echo '+dirty')" > "$OUT/GOVERNOR_VERSION"
echo "vendored governor report embed $(cat "$OUT/GOVERNOR_VERSION")"
