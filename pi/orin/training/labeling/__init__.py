"""The expert-sailor labeling surface — multi-labeler ranker + preference store + snapshot render.

Sailors rank candidate briefs (best→worst) and flag each one's confidence/urgency calibration; the
store keeps the full ranking (not collapsed pairs) keyed by labeler, so the reward-model flywheel
(Plan §6) has the richer signal when volume grows. Build for many labelers from day one: accounts,
a queue with deliberate overlap for inter-rater agreement, and gold-trap snapshots.
"""
