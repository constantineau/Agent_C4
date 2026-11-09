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

## What may be lost, and what proof comes first (Cole, 2026-11-09, #243)

- **The uplink queue has no cap and never evicts.** In an outage every batch waits on disk until
  the VPS accepts it (or rejects it for good: then it is quarantined, still on disk). Replay is
  paged so a big backlog never holds up the live data. In a very long outage the archiver's disk
  guard is the backstop, not a drop-oldest rule.
- **Nothing is deleted locally until the off-boat copy is proven:** its sha256 matches the local
  file AND a restore of it checks out (the copy is read back and hashed). `copyoff.py` (receiver
  re-hashes and re-reads every row) and `tools/backup/delete_archived.sh` (full restore, hashed)
  both work this way.
  **Not yet true of the archiver's retention prune:** it is gated on the copy-off ledger only
  when `ARCHIVE_OFFBOAT_LEDGER` is set (spring deploy step 5 in `pi/archiver/README.md`). Until
  then, readings outside a race session are still pruned after `ARCHIVE_RETAIN_DAYS` with no
  off-boat copy.
