---
status: accepted
---

# Learning proposes; a human approves before the boat model changes

The Lab-4 learning loop scores real tracks against the hindsight-optimal re-route and
derives refinements to polars, helm coefficients, and wave coefficients — but it
**never writes them into the boat model**. Every proposal is presented in the Debrief
for a person to approve or reject.

Auto-tuning was the obvious design and was rejected: the boat model is the input to
every future route, so a bad inference (a current-assisted leg read as extra boat
speed, a soft rating, one windy race over-weighted) would silently poison every
subsequent plan, and the error would be invisible precisely because the plans would
still look confident. Polar mining regularly reports >100% of target, which is usually
current or a soft certificate rather than performance — exactly the judgment call a
human has to make.

## Consequences

The loop's value is bounded by how often someone reviews proposals; that review is a
real workflow step, not overhead to optimize away. Approved changes should carry
provenance so a later debrief can tell measured refinements from certificate values.
