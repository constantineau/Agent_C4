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
  - the SQL seed and `shared/source_policy.DEFAULT_PRIORITY` agree, channel by channel and
    rank by rank — the boat reads the Python, the cloud reads the table, and nothing else
    stops them drifting apart
  - `CHANNEL_FOR_PATH` covers exactly the paths `tools.PRESENT` publishes
  - failover, unranked passthrough, and replay-safe (relative, not wall-clock) freshness

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_source_priority.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vps", "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import n2k_sources, source_policy   # noqa: E402

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
check("the policy's own matchers resolve too",
      n2k_sources.unresolved(source_policy.all_matchers(), DEV, NON_BUS_LABELS) == [])

# --- SQL seed vs the committed policy the boat uses -------------------------
print("source_priority.sql and shared/source_policy agree:")
check("same channels", sorted(seed) == sorted(source_policy.DEFAULT_PRIORITY))
for ch in sorted(seed):
    check(f"  {ch}: {seed[ch]}", seed[ch] == source_policy.DEFAULT_PRIORITY.get(ch))

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
print("OnboardSource._prefer (failover, passthrough, replay-safe freshness):")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")
from app.datasource_onboard import OnboardSource   # noqa: E402

prefer = OnboardSource._prefer.__get__(object.__new__(OnboardSource))
ORCA, GPS, PILOT, GHC = ("n2k-socketcan.15", "n2k-socketcan.3",
                         "n2k-socketcan.1", "n2k-socketcan.6")
T = 1_752_000_000.0     # any fixed epoch; only differences matter

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
# the replay trap: a decade-old archive must still select by priority
OLD = 1_405_000_000.0   # 2014, the bench sample log's era
check("freshness is relative, so a 2014 archive still honours priority",
      prefer("heel", [(ORCA, OLD - 3, 1.0), (GPS, OLD, 2.0)])[2] == 1.0)
check("...and its failover still fires on a 45 s gap",
      prefer("heel", [(ORCA, OLD - 60, 1.0), (GPS, OLD, 2.0)])[2] == 2.0)
check("exactly 45 s old still counts as fresh (boundary is inclusive)",
      prefer("heel", [(ORCA, T - 45, 1.0), (GPS, T, 2.0)])[2] == 1.0)

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
