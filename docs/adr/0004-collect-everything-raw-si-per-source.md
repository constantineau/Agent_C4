---
status: accepted
---

# Store every reading, per source, in raw SI

The uplink forwards **every** Signal K `(source, path)` reading verbatim to
`telemetry_raw(time, boat_id, source, path, value)` in raw SI units, and the Pi keeps
its own independent full-resolution archive. Redundant sources are all kept rather
than resolved to one "best" value at write time; `source_priority` ranks a preferred
source per channel at **read** time, with automatic failover and a `fell_back` flag.

Filtering, unit conversion, or picking a winner on ingest destroys the evidence needed
later: cross-checking disagreeing instruments, diagnosing a drifting sensor, or
re-deriving a signal a different way in a debrief months afterward. Storage is cheap;
a race you can't reconstruct is not.

## Consequences

Volume is high — hence session-scoped retention (only owner-declared race windows are
kept long-term and backfilled; everything else prunes locally) rather than filtering at
the source. Readers must be explicitly skeptical: cross-check redundant sources, flag
staleness and disagreement, never trust a lone value. Any code that converts units or
collapses sources belongs on the read path, never the write path.
