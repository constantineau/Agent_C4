---
status: accepted
---

# Deterministic compute onboard, frontier compute ashore (the bright line)

Racing rules treat customized advice arriving from off the boat while racing as
prohibited outside help (2026 Bayview Mackinac NOR §2.1(d); full memo:
`docs/RRS41_COMPLIANCE.md`), while the boat's own computer crunching its own sensors is
Expedition-class and legal. So the system is split three ways: a deterministic
no-LLM engine on the Pi (Tier 1, legal in-race), an LLM copilot on the Orin that only
narrates and condition-matches (Tier 2), and cloud/frontier work that happens
pre-start and is **frozen at the gun** (Tier 3). In a race the boat never phones the
cloud for a route, and the cloud's advice tools are race-gated fail-closed.

## Considered options

The obvious architecture — cloud does the strategy, the boat is a thin client — is
faster to build and was rejected outright: it would make the boat's whole advantage
illegal, which is worse than not having it.

## Consequences

Logic is deliberately duplicated: routing/tactics/sails exist both in the cloud Lab
and in the onboard engine, sharing modules where possible but running against
different data sources (`datasource.active()`). Everything the boat needs in-race
must therefore be compiled ashore and loaded frozen — the "homework" pattern
(playbook, obstacles, fleet roster, forecast fingerprint, venue stats, course,
checklists). A feature that needs fresh cloud compute mid-race cannot be built;
it must be reshaped into homework plus onboard evaluation.
