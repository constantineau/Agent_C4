"""Sensor-priority policy — it must actually BIND, and it must not drop data.

Regression test for the 2026-09-07 finding: `source_priority` (migration 003) and
`source_notes` are written in device terms (`orca`, `24xd`, `reactor`, `gnd`, `943`) but
Signal K `$source` labels are N2K addresses (`n2k-socketcan.15`), and the selector tested
`match in source.lower()`. No matcher could ever match, on any channel, so every read silently
took the "freshest source wins" fallback — including a whole race on the sensor the policy
itself annotates "non-racing only". An unresolvable matcher and a satisfied one produced
identical output, which is why it survived a year of use.

So the assertions here are mostly about *bindability*, not about ranking taste:

  - every matcher in the SQL seed resolves to a device on this boat's bus (or to a real
    non-bus source label like `derived-data`)
  - the SQL seed and the boat's profile (`hardware.source_priority` in vps/lab/boats/sr33.json,
    #313) agree, channel by channel and rank by rank — the boat reads the profile, the cloud
    reads the table, and nothing else stops them drifting apart
  - `CHANNEL_FOR_PATH` covers exactly the paths `tools.PRESENT` publishes
  - failover, unranked passthrough, and replay-safe (relative, not wall-clock) freshness

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_source_priority.py
"""
import os
os.environ["BOAT_ID"] = "sr33"   # these checks are about sr33's own N2K devices (no default boat, #307)
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vps", "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import n2k_sources, source_policy   # noqa: E402
# the reference boat's own sensor priority, from its profile (#313): these checks are about its bus
import json as _json   # noqa: E402
from shared import boat_hardware as _BH, source_policy as _SP   # noqa: E402
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                       "vps", "lab", "boats", "sr33.json")) as _f:
    SR33_PRIORITY = _BH.source_priority(_json.load(_f))
_SP.set_priority(SR33_PRIORITY)

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


SEED = os.path.join(_ROOT, "vps", "db", "seed", "source_priority.sql")
NOTES = os.path.join(_ROOT, "vps", "db", "seed", "source_notes.sql")
DEV = n2k_sources.SR33_DEVICES

# Non-bus sources that legitimately appear in telemetry: the engine's own derived output and
# Signal K's course provider. Measured against telemetry_raw 2026-09-07.
NON_BUS_LABELS = ("derived-data", "course-provider", "courseApi", "crew")


def seed_rows(path):
    """[(channel, rank, match)] parsed out of the source_priority seed."""
    text = open(path).read()
    rows = re.findall(r"\('sr33','([a-z_]+)',(\d+),'([a-z0-9_]+)'", text)
    return [(c, int(r), m) for c, r, m in rows]


def seed_priority(path):
    """channel -> matchers in rank order, as the DB would load it."""
    prio = {}
    for channel, rank, match in seed_rows(path):
        prio.setdefault(channel, []).append((rank, match))
    return {c: [m for _, m in sorted(v)] for c, v in prio.items()}


# --- the device map ---------------------------------------------------------
print("n2k_sources identity + matching:")
check("the Orca Core resolves", n2k_sources.resolve("n2k-socketcan.15", DEV)["model"] == "Orca Core")
check("'orca' matches the Orca Core's address",
      n2k_sources.matches("n2k-socketcan.15", "orca", DEV))
check("'orca' does NOT match the autopilot",
      not n2k_sources.matches("n2k-socketcan.1", "orca", DEV))
check("'reactor' matches the Reactor 40", n2k_sources.matches("n2k-socketcan.1", "reactor", DEV))
check("'24xd' matches the GPS24xd", n2k_sources.matches("n2k-socketcan.3", "24xd", DEV))
check("'gnd' matches the GND10 (the masthead's bridge)",
      n2k_sources.matches("n2k-socketcan.0", "gnd", DEV))
check("'943' matches the GPSMAP 943", n2k_sources.matches("n2k-socketcan.11", "943", DEV))
check("'b951' matches the em-trak AIS box", n2k_sources.matches("n2k-socketcan.43", "b951", DEV))
check("'intelliducer' matches the depth transducer",
      n2k_sources.matches("n2k-socketcan.2", "intelliducer", DEV))
# the whole bug, as a single assertion
check("the OLD bare-label test would have matched nothing",
      not any("orca" in s.lower() for s in DEV))
# serial digits must not become a matcher surface
check("a serial's digits are not matchable",
      not n2k_sources.matches("n2k-socketcan.15", "d77c42", DEV))
check("unmapped sources fall back to the raw label",
      n2k_sources.matches("derived-data", "derived", DEV))
check("an unmapped boat matches on labels only, never crashes",
      n2k_sources.matches("n2k-socketcan.15", "n2k", {}) and
      not n2k_sources.matches("n2k-socketcan.15", "orca", {}))
check("empty matcher never matches", not n2k_sources.matches("n2k-socketcan.15", "", DEV))

# --- bindability: the assertion this bug needed -----------------------------
print("every seeded matcher resolves to something real:")
seed = seed_priority(SEED)
seed_matchers = sorted({m for ms in seed.values() for m in ms})
missing = n2k_sources.unresolved(seed_matchers, DEV, NON_BUS_LABELS)
check(f"source_priority.sql: no unresolvable matchers (got {missing})", missing == [])

note_matchers = re.findall(r"\('sr33','([a-z0-9_]+)',", open(NOTES).read())
note_missing = [m for m in n2k_sources.unresolved(note_matchers, DEV, NON_BUS_LABELS)
                if m != "gwind"]   # gwind is documented as NOT a distinct bus source
check(f"source_notes.sql: no unresolvable matchers besides gwind (got {note_missing})",
      note_missing == [])
check("'gdt' is gone from both seeds — there is no GDT 43 on this bus",
      "gdt" not in seed_matchers and "gdt" not in note_matchers)
check("'gwind' is no longer a PRIORITY matcher (it cannot resolve)",
      "gwind" not in seed_matchers)
# Removed 2026-09-08. `b951` resolves perfectly well — that is the point. The em-trak's own-ship
# fix is real data, and ranking it for lat/lon/sog/cog is still the AIS contamination bug written
# down as an intention, protected only by a filter in a different module. If a future session
# re-adds it "for redundancy", this is the assertion that should stop it.
check("'b951' is not a PRIORITY matcher anywhere — the AIS box does not lead own-ship",
      "b951" not in seed_matchers and "b951" not in source_policy.all_matchers())
check("...and the three GPS channels still have real redundancy without it",
      all(len(source_policy.matchers_for(c)) == 3 for c in ("sog", "cog", "lat", "lon")))
check("the policy's own matchers resolve too",
      n2k_sources.unresolved(source_policy.all_matchers(), DEV, NON_BUS_LABELS) == [])

# --- SQL seed vs the committed policy the boat uses -------------------------
print("source_priority.sql and the boat's profile (hardware.source_priority) agree:")
check("same channels", sorted(seed) == sorted(SR33_PRIORITY))
for ch in sorted(seed):
    check(f"  {ch}: {seed[ch]}", seed[ch] == SR33_PRIORITY.get(ch))

print("CHANNEL_FOR_PATH matches tools.PRESENT:")
present = re.findall(r'"([a-zA-Z0-9.]+)":\s*\("([a-z_]+)"',
                     open(os.path.join(_ROOT, "vps", "agent", "app", "tools.py")).read())
check(f"same path set ({len(present)} paths)",
      sorted(p for p, _ in present) == sorted(source_policy.CHANNEL_FOR_PATH))
check("same path->channel mapping",
      all(source_policy.CHANNEL_FOR_PATH.get(p) == c for p, c in present))

# --- rank_for / matchers_for ------------------------------------------------
print("rank_for:")
check("heel: Orca leads", source_policy.rank_for("heel", "n2k-socketcan.15", DEV) == 0)
check("heel: 24xd is rank 2", source_policy.rank_for("heel", "n2k-socketcan.3", DEV) == 1)
check("heel: the autopilot is last",
      source_policy.rank_for("heel", "n2k-socketcan.1", DEV) == 2)
check("aws: the GND10 masthead leads the Orca",
      source_policy.rank_for("aws", "n2k-socketcan.0", DEV) == 0
      and source_policy.rank_for("aws", "n2k-socketcan.15", DEV) == 1)
check("an unranked source is None, not last",
      source_policy.rank_for("heel", "n2k-socketcan.6", DEV) is None)
check("a path resolves like its channel",
      source_policy.matchers_for("navigation.attitude.roll") ==
      source_policy.matchers_for("heel"))
check("an unranked path yields no matchers",
      source_policy.matchers_for("navigation.gnss.satellites") == [])

# --- selection: _prefer, without touching a database ------------------------
print("OnboardSource._prefer (failover, passthrough, wall-clock freshness):")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")
from app.datasource_onboard import OnboardSource   # noqa: E402

prefer = OnboardSource._prefer.__get__(object.__new__(OnboardSource))
ORCA, GPS, PILOT, GHC = ("n2k-socketcan.15", "n2k-socketcan.3",
                         "n2k-socketcan.1", "n2k-socketcan.6")
T = 1_752_000_000.0     # any fixed epoch; the policy's clock is pinned to it below


class _Clock:
    """Stands in for `source_policy._time`: ages are judged against the wall clock (#221), so the
    fixtures pin it rather than race the real one."""
    at = T

    @classmethod
    def time(cls):
        return cls.at


_real_clock, source_policy._time = source_policy._time, _Clock

check("no candidates -> None", prefer("heel", []) is None)
check("lead source wins even when a lower rank is fresher",
      prefer("heel", [(ORCA, T - 3, 1.0), (GPS, T, 2.0), (PILOT, T, 3.0)])[2] == 1.0)
check("lead stale beyond 45 s -> next rank, not the freshest",
      prefer("heel", [(ORCA, T - 60, 1.0), (GPS, T - 5, 2.0), (PILOT, T, 3.0)])[2] == 2.0)
check("lead silent -> next rank",
      prefer("heel", [(GPS, T - 5, 2.0), (PILOT, T, 3.0)])[2] == 2.0)
check("all ranked sources stale -> freshest available (data is never dropped)",
      prefer("heel", [(ORCA, T - 600, 1.0), (GHC, T, 9.0)])[2] == 9.0)
check("unranked-only candidates pass through",
      prefer("heel", [(GHC, T, 9.0)])[2] == 9.0)
check("unranked path -> freshest wins",
      prefer("navigation.gnss.satellites", [(GPS, T - 5, 1.0), (GHC, T, 2.0)])[2] == 2.0)
check("apparent wind takes the masthead over the Orca",
      prefer("environment.wind.speedApparent", [(ORCA, T, 9.0), ("n2k-socketcan.0", T, 8.0)])[2]
      == 8.0)
check("exactly 45 s old still counts as fresh (boundary is inclusive)",
      prefer("heel", [(ORCA, T - 45, 1.0), (GPS, T, 2.0)])[2] == 1.0)

# #221: ONE freshness rule. `/conditions` judges a 45 s age on the wall clock; this pick used to
# judge it against the newest sample, so the two named different leads for the same readings.
print("one freshness rule with /conditions (#221):")
from app import onboard_conditions as oc   # noqa: E402
TWD_P, DERIVED = "environment.wind.directionTrue", "derived-data"
cands = [(ORCA, T - 90, 200.0), (DERIVED, T - 60, 210.0)]   # Codex's repro: both quiet
pick = prefer(TWD_P, cands)
check("Orca 90 s + derived 60 s: both stale, so the freshest (derived) leads, not the Orca",
      pick[0] == DERIVED)
oc._age = lambda epoch: round(T - epoch, 1)
best, _why, _fb = oc._choose_preferred("twd", [{"source": s, "value": v, "age_s": T - t}
                                               for s, t, v in cands])
check("...the same lead `/conditions` names", best["source"] == pick[0])
check("a lead 30 s behind a newer rank-2 sample but 50 s old is stale, not 'current'",
      prefer("heel", [(ORCA, T - 50, 1.0), (GPS, T - 20, 2.0)])[2] == 2.0)
check("an explicit `now` overrides the clock",
      source_policy.preferred("heel", [(ORCA, T - 50, 1.0), (GPS, T - 20, 2.0)], DEV,
                              now=T - 10)[2] == 1.0)
check("the window pick follows the same rule (series_publisher)",
      source_policy.series_publisher(TWD_P, [(ORCA, T - 300 + i, 0.0) for i in range(211)]
                                     + [(DERIVED, T - 120 + i, 0.0) for i in range(61)],
                                     DEV) == DERIVED)

# replay: the rig freezes the process clock at the frame's time, so priority still holds there
OLD = 1_405_000_000.0   # 2014, the bench sample log's era
_Clock.at = OLD
check("a 2014 archive replayed with the clock at its time still honours priority",
      prefer("heel", [(ORCA, OLD - 3, 1.0), (GPS, OLD, 2.0)])[2] == 1.0)
check("...and its failover still fires on a 45 s gap",
      prefer("heel", [(ORCA, OLD - 60, 1.0), (GPS, OLD, 2.0)])[2] == 2.0)
_Clock.at = T
check("read against today's clock, the same 2014 rows are all stale -> freshest, never dropped",
      prefer("heel", [(ORCA, OLD - 3, 1.0), (GPS, OLD, 2.0)])[2] == 2.0)
source_policy._time = _real_clock

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
