"""The race window, derived from the RECORD rather than from button presses.

Every shore-side analysis of a race is bounded by a session window: the debrief's own-log track
source reads one, the archiver's retention prune keeps one, and the learning loop is fed whatever
falls inside it. Until now that window was exactly the interval between two taps on the console's
⏺ LOG button, and the Jul 18 2026 race is what that costs:

    18:52:20.81Z   session end_ts written        <- the button
    18:52:25Z      A3 + J1        kite hoisted
    18:52:26Z      A3             J1 dropped
    18:52:29Z      A3 + SS        staysail up

Four taps on the sail bar in eleven seconds and the recording stopped 4.2 s before the first of
them. Nobody ended that race at 18:52 — they sailed on for five more hours, lost the house bank,
had the compass kicked, and retired. The marker claims **1 h 49 m of a 7 h race**. Of the three
sessions ever recorded, two look accidental (Jul 8 lasted 21 seconds).

So a button press is a HINT about a race, not its definition. This module treats the markers as
seeds and derives the real window from continuous, underway telemetry:

    1. SEED     — session markers, optionally filtered to one `race_id`.
    2. STITCH   — merge markers separated by less than `STITCH_GAP_S`. A race interrupted by a
                  mis-tap (stop at 18:52, start again at 18:55) is one race, not two.
    3. EXTEND   — grow both edges across telemetry that is continuous and shows the boat moving.
                  This is what recovers the five hours nobody pressed a button for.
    4. BOUND    — stop at the TURNAROUND: a sustained reversal of course made good that never
                  comes back. Without this, Jul 18 extends to 07:15Z and calls seven hours of
                  motoring home "the race" — and the owner's standing instruction is that
                  everything after the turnaround is scrap.

Every step records WHY in `provenance`, and the raw marker is always reported alongside the
derived window. A window that silently disagrees with what the crew remembers pressing is worse
than no window at all; the caller and the debrief UI are expected to show both.

Pure functions over plain samples so the Lab, the cloud agent and the tests all see identical
behaviour, and so a fixture of the real race can drive it with no database.
"""
import os

from . import n2k_sources
from . import source_policy

# A mis-tap and its correction are minutes apart; two genuine races on one day are hours apart.
STITCH_GAP_S = float(os.environ.get("RACE_WINDOW_STITCH_GAP_S", "3600"))
# Below this the boat is parked, not racing. Same 3 kn the heading cross-check uses to decide COG
# means anything — under way is under way.
UNDERWAY_KN = float(os.environ.get("RACE_WINDOW_UNDERWAY_KN", "1.5"))
# Telemetry silence longer than this is a different sitting of the boat, not a lull.
MAX_GAP_S = float(os.environ.get("RACE_WINDOW_MAX_GAP_S", "1800"))
# A turn only ends a race if it is big and it STICKS. Jul 18 has a 151-deg transient at 23:20Z
# while the crew re-seated the kicked GPS, and the boat was back on 24 deg twenty minutes later;
# a detector without a sustain requirement ends the race there and loses the last hour.
TURN_MIN_DEG = float(os.environ.get("RACE_WINDOW_TURN_MIN_DEG", "120"))
TURN_SUSTAIN_S = float(os.environ.get("RACE_WINDOW_TURN_SUSTAIN_S", "2700"))
# How much course to average on each side of a candidate turn.
TURN_REF_S = float(os.environ.get("RACE_WINDOW_TURN_REF_S", "1800"))


def choose_motion_source(by_source, devices=None, channel="sog"):
    """Which publisher's series to read the race window off. Returns (label, resolved).

    A window is derived from ONE source for the same reason the heading cross-check is: a series
    that alternates between two GPSs is a series of manufactured turns, and `find_turnaround`
    would read them as the end of a race.

    Order: sources that resolve to a real device on this boat first, then policy rank, then
    coverage, then label. The device check is not decoration — the Jul 8 2026 session sits in a
    window where the only motion publishers are `n2k-sample-data.160` and `fake-seed`, the bench
    stack's replayed 2014 log. Unranked is not the same as not-this-boat, and picking the second
    silently would derive a race window from a sample file. When nothing resolves the best
    available is still returned — never drop data — and `resolved` is False so the caller can say
    so out loud."""
    if not by_source:
        return None, False
    devices = devices or {}

    def key(s):
        rank = source_policy.rank_for(channel, s, devices)
        return (0 if n2k_sources.resolve(s, devices) else 1,
                99 if rank is None else rank, -len(by_source[s]), s)

    best = min(by_source, key=key)
    return best, bool(n2k_sources.resolve(best, devices))


def _wrap180(deg):
    return (deg + 540.0) % 360.0 - 180.0


def _mean_angle(degs):
    """Circular mean. A plain average of 359 and 1 gives 180, and this project has been bitten by
    exactly that more than once."""
    if not degs:
        return None
    import math
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if x == 0.0 and y == 0.0:
        return None
    return math.degrees(math.atan2(y, x))


def stitch(sessions, gap_s=None):
    """Merge session markers closer together than `gap_s`. Returns [{start_ts, end_ts, ids}].

    `sessions` is [{start_ts, end_ts, id?}]; markers with no `end_ts` (a window still open) take
    their start as the end so an unclosed session is still a point on the timeline rather than
    being dropped — losing an in-progress race is the failure this module exists to prevent."""
    gap_s = STITCH_GAP_S if gap_s is None else gap_s
    norm = []
    for s in sessions or ():
        a = s.get("start_ts")
        if a is None:
            continue
        b = s.get("end_ts")
        norm.append((float(a), float(a if b is None else b), s.get("id")))
    norm.sort()
    out = []
    for a, b, sid in norm:
        if out and a - out[-1]["end_ts"] <= gap_s:
            out[-1]["end_ts"] = max(out[-1]["end_ts"], b)
            out[-1]["ids"].append(sid)
        else:
            out.append({"start_ts": a, "end_ts": b, "ids": [sid]})
    return out


def underway_span(motion, at_ts, underway_kn=None, max_gap_s=None):
    """The largest interval around `at_ts` over which the boat is moving and the record is
    continuous. `motion` is [(epoch_s, sog_kn, cog_deg_or_None)], time-ordered.

    Returns (start_ts, end_ts) or None when `at_ts` sits outside any underway run. Walks outward
    from the sample nearest `at_ts` rather than scanning for runs, so a race that begins during a
    pre-start drift is not merged with the previous day's sail."""
    underway_kn = UNDERWAY_KN if underway_kn is None else underway_kn
    max_gap_s = MAX_GAP_S if max_gap_s is None else max_gap_s
    rows = [r for r in (motion or ()) if r[1] is not None]
    if not rows:
        return None
    i = min(range(len(rows)), key=lambda j: abs(rows[j][0] - at_ts))
    if rows[i][1] < underway_kn:
        # `at_ts` itself is a stopped sample — accept it only if a moving sample sits within one
        # gap, which is the normal case for a start-line press made while drifting.
        near = [j for j in range(len(rows))
                if abs(rows[j][0] - at_ts) <= max_gap_s and rows[j][1] >= underway_kn]
        if not near:
            return None
        i = min(near, key=lambda j: abs(rows[j][0] - at_ts))
    lo = i
    while lo > 0 and rows[lo - 1][1] >= underway_kn and rows[lo][0] - rows[lo - 1][0] <= max_gap_s:
        lo -= 1
    hi = i
    while (hi + 1 < len(rows) and rows[hi + 1][1] >= underway_kn
           and rows[hi + 1][0] - rows[hi][0] <= max_gap_s):
        hi += 1
    return rows[lo][0], rows[hi][0]


def find_turnaround(motion, after_ts, min_deg=None, sustain_s=None, ref_s=None):
    """First moment after `after_ts` where course made good reverses and STAYS reversed.

    Returns {ts, from_deg, to_deg, change_deg} or None. The sustain requirement is the whole
    design: on Jul 18 the boat swung 151 deg at 23:20Z while the crew wrestled the kicked GPS back
    into its mount and was on 24 deg again by 23:40 — a detector that fires on the first big
    change calls the race over an hour early and throws away the part that matters."""
    min_deg = TURN_MIN_DEG if min_deg is None else min_deg
    sustain_s = TURN_SUSTAIN_S if sustain_s is None else sustain_s
    ref_s = TURN_REF_S if ref_s is None else ref_s
    rows = [r for r in (motion or ()) if len(r) > 2 and r[2] is not None and r[0] >= after_ts]
    for k, (t, _sog, _cog) in enumerate(rows):
        before = [r[2] for r in rows[:k] if t - r[0] <= ref_s]
        after = [r[2] for r in rows[k:] if r[0] - t <= ref_s]
        if len(before) < 3 or len(after) < 3:
            continue
        a, b = _mean_angle(before), _mean_angle(after)
        if a is None or b is None or abs(_wrap180(b - a)) < min_deg:
            continue
        # It reversed. Did it stay reversed? Every later sample within the sustain horizon must
        # still be far from the pre-turn course.
        held = [r[2] for r in rows[k:] if r[0] - t <= sustain_s]
        if len(held) < 3 or any(abs(_wrap180(c - a)) < min_deg / 2.0 for c in held):
            continue
        return {"ts": t, "from_deg": round(a, 1), "to_deg": round(b, 1),
                "change_deg": round(abs(_wrap180(b - a)), 1)}
    return None


def derive(sessions, motion, race_id=None, anchor_ts=None, stop_at_turnaround=True, **kw):
    """The race window the record supports, with the reasoning that produced it.

    Returns {start_ts, end_ts, marker_start_ts, marker_end_ts, provenance:[...], turnaround}.
    Returns None only when there is no usable seed at all — a caller with a marker always gets a
    window back, in the worst case the marker itself.

    `anchor_ts` picks WHICH stitched group to derive when several survive the `race_id` filter,
    and callers deriving a window for a specific session must pass it. Every session this boat
    has ever recorded carries the same `race_id` — a regatta id, not a per-race one — so
    filtering alone leaves three windows across eleven days and "the longest" would answer a
    question nobody asked."""
    if race_id is not None:
        sessions = [s for s in (sessions or ()) if s.get("race_id") == race_id]
    merged = stitch(sessions, kw.get("gap_s"))
    if not merged:
        return None
    if anchor_ts is None:
        seed = max(merged, key=lambda w: w["end_ts"] - w["start_ts"])
    else:
        anchor_ts = float(anchor_ts)
        # containing group first, else the nearest edge — an anchor from a marker whose window
        # was widened by an earlier stitch still lands on its own race.
        inside = [w for w in merged if w["start_ts"] <= anchor_ts <= w["end_ts"]]
        seed = (inside[0] if inside else
                min(merged, key=lambda w: min(abs(w["start_ts"] - anchor_ts),
                                              abs(w["end_ts"] - anchor_ts))))
    prov = []
    if len(merged) < len([s for s in sessions if s.get("start_ts") is not None]):
        prov.append(f"stitched {len(seed['ids'])} session marker(s) "
                    f"{[i for i in seed['ids'] if i is not None]} separated by less than "
                    f"{(kw.get('gap_s') or STITCH_GAP_S) / 60:.0f} min")
    start, end = seed["start_ts"], seed["end_ts"]
    marker_start, marker_end = start, end

    span = underway_span(motion, start, kw.get("underway_kn"), kw.get("max_gap_s"))
    if span:
        if span[1] > end:
            prov.append(f"extended the end by {(span[1] - end) / 3600:.1f} h — the boat was still "
                        f"under way and the record is continuous, so the marker ended mid-race")
            end = span[1]
        # The START is deliberately NOT extended by default, and the asymmetry is the point: a
        # start press is made at the gun, with the whole crew watching for it, while the stop is
        # the one that gets caught by a sleeve during a sail change. Extending backwards would
        # pull the delivery to the line into the race — on Jul 18 that is 104 minutes of motoring
        # at 1-4 kn, and it would land in the learning loop's polar bins as racing.
        if kw.get("extend_start") and span[0] < start:
            prov.append(f"extended the start back {(start - span[0]) / 60:.0f} min to the "
                        f"beginning of continuous underway telemetry (extend_start)")
            start = span[0]
    else:
        prov.append("no underway telemetry around the marker — using the marker as recorded")

    turn = find_turnaround(motion, marker_end, kw.get("min_deg"), kw.get("sustain_s"),
                           kw.get("ref_s")) if stop_at_turnaround else None
    if turn and turn["ts"] > start:
        # `min`, not assignment: on Jul 18 the underway run already ends at 00:25 because the boat
        # slowed below 1.5 kn to douse before turning, so the bound changes nothing here. It is
        # still required — a boat that gybes away and keeps its speed up would sail straight past
        # the end of the race with the underway test none the wiser.
        if turn["ts"] < end:
            prov.append(f"ended at the turnaround — course made good reversed "
                        f"{turn['change_deg']:.0f}° ({turn['from_deg']:.0f}° → "
                        f"{turn['to_deg']:.0f}°) and stayed reversed; everything after it is "
                        f"delivery, not racing")
        else:
            prov.append(f"turnaround detected {(turn['ts'] - end) / 60:.0f} min after the boat "
                        f"had already slowed out of the underway window — consistent, no change")
        end = min(end, turn["ts"])

    return {"start_ts": start, "end_ts": end,
            "marker_start_ts": marker_start, "marker_end_ts": marker_end,
            "session_ids": [i for i in seed["ids"] if i is not None],
            "turnaround": turn, "provenance": prov,
            "hours": round((end - start) / 3600.0, 2),
            "marker_hours": round((marker_end - marker_start) / 3600.0, 2)}
