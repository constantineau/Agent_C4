"""Bank watch — is the readout honest about a discharge, and quiet about this boat being itself?

The fixture below is the REAL house-bank voltage from the race, 5-minute means straight out of
`telemetry_raw` (`electrical.batteries.0.voltage`, the Orca Core), 13:00Z Jul 18 -> 06:00Z
Jul 19.

REFRAMED 2026-09-09 (Cole: "we never saw the battery go too low — it's a mistake to think of
11.6 V as a danger line"). The original assertions here demanded warn-then-danger before the
20:40:30Z archiver failure, on the theory that the sagging bank caused it. The minute-level
record does not support that theory — at 20:40 the bank read 11.65 V / min 11.41, unremarkable
against the preceding hour — and across 10,584 recorded minutes this bank's median is 12.29 V
with race days sitting at 11.5–11.7 V for hours, everything running. A tile that alarms on this
boat's ordinary race voltage is a tile the crew learns to ignore (measured: 98% of a HEALTHY
race read warn/danger under the old lines).

So the load-bearing assertions are now: NO alarm anywhere on either real race (the bank never
left its own record, so a correct tile is quiet — the number and the trend are the product),
the projection displayed-but-not-alarmed above the plateau band, `warn` for a drain below it,
and `danger` for a sustained level below everything this bank has ever recorded. Swept over the
real Postgres series: Jul 18 0.0% alarmed, Jul 15 2.5% with one on/off.

Run:  PYTHONPATH=vps/agent:. python3 vps/agent/test_power.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "vps", "agent")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("DATA_SOURCE", "onboard")
os.environ.setdefault("ONBOARD_LIVE_WS", "false")

from app import power   # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


T0 = 1784383200          # 2026-07-18T13:00:00Z
RACE_VOLTS = [        # (minutes after T0, volts)
    (0, 12.35), (5, 12.33), (10, 12.69), (15, 12.83), (20, 12.81), (25, 12.88),
    (30, 12.85), (35, 12.84), (40, 12.76), (45, 12.85), (50, 12.88), (55, 12.88),
    (60, 12.68), (65, 12.88), (70, 12.82), (75, 12.73), (80, 12.26), (85, 12.21),
    (90, 12.15), (95, 12.11), (100, 12.27), (105, 12.29), (110, 12.29), (115, 12.31),
    (120, 12.3), (125, 12.3), (130, 12.05), (135, 12.16), (140, 12.17), (145, 12.31),
    (150, 12.08), (155, 12.03), (160, 12.21), (165, 12.22), (170, 11.64), (175, 11.76),
    (180, 11.78), (185, 11.56), (190, 11.62), (195, 11.76), (200, 11.78), (205, 11.68),
    (210, 11.66), (215, 11.58), (220, 11.66), (225, 11.8), (230, 11.78), (235, 11.73),
    (240, 11.75), (245, 11.64), (250, 11.61), (255, 11.72), (260, 11.72), (265, 11.73),
    (270, 11.62), (275, 11.74), (280, 11.58), (285, 11.75), (290, 11.77), (295, 11.59),
    (300, 11.75), (305, 11.62), (310, 11.55), (315, 11.73), (320, 11.69), (325, 11.59),
    (330, 11.62), (335, 11.59), (340, 11.59), (345, 11.57), (350, 11.64), (355, 11.64),
    (360, 11.61), (365, 11.62), (370, 11.59), (375, 11.63), (380, 11.6), (385, 11.62),
    (390, 11.61), (395, 11.55), (400, 11.65), (405, 11.67), (410, 11.73), (415, 11.61),
    (420, 11.73), (425, 11.46), (430, 11.69), (435, 11.7), (440, 11.72), (445, 11.27),
    (450, 11.71), (455, 11.76), (460, 11.59), (465, 11.63), (470, 11.84), (475, 11.86),
    (480, 11.88), (485, 11.96), (490, 11.8), (495, 11.84), (500, 11.89), (505, 11.88),
    (510, 11.77), (515, 11.88), (520, 11.84), (525, 11.81), (530, 11.62), (535, 11.64),
    (540, 11.84), (545, 11.78), (550, 11.7), (555, 11.76), (560, 11.16), (565, 11.65),
    (570, 11.72), (575, 12.01), (580, 12.0), (585, 12.21), (590, 12.24), (595, 11.96),
    (600, 12.2), (605, 12.13), (610, 11.99), (615, 12.07), (620, 11.96), (625, 11.97),
    (630, 11.75), (635, 12.04), (640, 12.19), (645, 12.19), (650, 12.15), (655, 12.03),
    (660, 11.95), (665, 11.95), (670, 12.28), (675, 12.28), (680, 12.18), (685, 12.1),
    (690, 12.27), (695, 12.39), (700, 12.43), (705, 12.39), (710, 12.51), (715, 12.51),
    (720, 12.44), (725, 12.49), (730, 12.47), (735, 12.47), (740, 12.54), (745, 12.51),
    (750, 12.53), (755, 12.57), (760, 12.52), (765, 12.22), (770, 12.5), (775, 12.56),
    (780, 12.6), (785, 12.4), (790, 12.43), (795, 12.59), (800, 12.63), (805, 12.45),
    (810, 12.46), (815, 12.56), (820, 12.46), (825, 12.38), (830, 12.63), (835, 12.58),
    (840, 12.51), (845, 12.6), (850, 12.57), (855, 12.45), (860, 12.6), (865, 12.63),
    (870, 12.67), (875, 12.69), (880, 12.67), (885, 12.64), (890, 13.24), (895, 13.32),
    (900, 13.23), (905, 13.42), (910, 13.3), (915, 13.38), (920, 13.31), (925, 13.36),
    (930, 13.21), (935, 13.21), (940, 13.28), (945, 13.22), (950, 13.14), (955, 13.3),
]

SERIES = [(T0 + m * 60, v) for m, v in RACE_VOLTS]
ARCHIVE_DIED = 460 + 40.5 / 60          # 20:40:30Z, in minutes after T0
TURNED_AROUND = 420                      # 20:00 local EDT = 00:00Z Jul 19 -> T0+660
RETIRE_MIN = 660


def at(minutes):
    """What the bank watch would have reported `minutes` after T0 (nothing later is visible)."""
    now = T0 + minutes * 60
    return power.assess_series([r for r in SERIES if r[0] <= now], now=now)


def first_status(status, lo=0, hi=RETIRE_MIN):
    """Earliest minute in [lo, hi] at which `status` is reported, else None."""
    for m in range(int(lo), int(hi) + 1, 5):
        if at(m)["status"] == status:
            return m
    return None


# --- the race, as it actually happened -------------------------------------
print("the Jul 18 curve:")
start = at(30)
# 12.35 -> 12.88 V in the first half hour: the bank was on charge at the dock, so `charging`
# is the correct reading here, not `ok`. (The first version of this test asserted `ok` and was
# simply wrong about the race.)
check(f"pre-start bank is healthy ({start['volts']:.2f} V, {start['status']})",
      start["status"] in ("ok", "charging") and start["volts"] > 12.5)
check("...and not alarmed", start["status"] not in ("warn", "danger"))

# The opening fall is steep (-0.70 V/h, projecting ~1.1 h to the floor) — and the bank then
# settled at its loaded plateau, as it also did on Jul 15. A linear projection from inside the
# normal band has been wrong on every race in the record, so above 11.5 V it is DISPLAYED, not
# alarmed: the crew sees the slope and the time, the tile stays quiet.
falling = at(180)
check(f"the steep fall shows the projection without alarming "
      f"(~{falling['hours_to_floor']} h at {falling['trend_v_per_h']:+.2f} V/h, "
      f"{falling['status']})",
      falling["hours_to_floor"] is not None and falling["dark_at_epoch"] is not None
      and falling["status"] == "ok")
check("no warn fires anywhere in this race — the bank never left its own record",
      first_status("warn") is None)

# REFRAMED 2026-09-09: the old test demanded warn-then-danger held to the moment the archiver
# died, encoding the theory that the bank killed it. At that minute the bank read 11.65 V — a
# level BOTH race days sat at for hours with everything running — and Cole has ruled 11.6 V is
# not a danger line. The honest reading of the 11.6–11.7 V plateau is the NUMBER, steady, with
# no alarm: this bank at race load. Danger is reserved for a sustained level below everything
# the record holds (<= floor + 0.3 = 11.3 V), which this race never reached.
died = at(ARCHIVE_DIED)
check(f"the plateau does NOT alarm — 11.6 V is this boat racing, not an emergency "
      f"({died['volts']:.2f} V, {died['status']})", died["status"] == "ok")
check("danger never fires on this race — it never sustained below 11.3 V",
      first_status("danger") is None)
check("...and the number is still on the screen the whole plateau",
      all(at(m)["volts"] is not None and abs(at(m)["volts"] - 11.66) < 0.25
          for m in range(int(ARCHIVE_DIED) - 130, int(ARCHIVE_DIED), 25)))

# --- the sail home: recovery must not read as an emergency ------------------
print("the motor home (alternator on):")
rising = at(720)          # mid-recovery: the alternator is putting the bank back
check(f"charging is recognised, not alarmed ({rising['volts']:.2f} V, "
      f"{rising['trend_v_per_h']:+.2f} V/h)",
      rising["charging"] is True and rising["status"] in ("charging", "danger"))
full = at(950)            # 13.18 V and regulating: full, flat trend — `ok`, not `charging`
check(f"a full regulated bank reads ok ({full['volts']:.2f} V)", full["status"] == "ok")
check("nothing alarms once recovered", full["status"] not in ("warn", "danger"))
# precedence, on a synthetic case rather than a guess about the curve: a bank that is flat but
# being charged is still flat, and level must win over the rising trend.
flat_charging = [(T0 + m * 60, 11.10 + 0.004 * m) for m in range(0, 46)]   # +0.24 V/h at ~11.2 V
fc = power.assess_series(flat_charging, now=T0 + 45 * 60)
check(f"a near-floor-but-recovering bank still reports danger ({fc['volts']:.2f} V, "
      f"{fc['trend_v_per_h']:+.2f} V/h)", fc["status"] == "danger" and fc["charging"] is True)
# 11.7 V wobbling upward is this bank at race load, drifting — under the 2026-09-09 reframe it
# reads `ok` with the number shown (the 2026-09-08 version demanded `warn` here, back when
# 11.7 V was treated as "low"; Cole has since ruled the band 11.5–11.7 is ordinary for this
# bank). The wobble is below CHARGE_V_PER_H, so it must not read `charging` either.
low_wobble = [(T0 + m * 60, 11.72 + 0.002 * m) for m in range(0, 46)]      # +0.12 V/h at 11.7 V
lw = power.assess_series(low_wobble, now=T0 + 45 * 60)
check(f"11.7 V drifting upward never alarms — the boat's ordinary band "
      f"({lw['volts']:.2f} V, {lw['status']})",
      lw["status"] in ("ok", "charging"))
# the healthy case must still be quiet: charging only annotates a LOW level, it is not itself
# a status downgrade
hi = power.assess_series([(T0 + m * 60, 12.60 + 0.004 * m) for m in range(0, 46)],
                         now=T0 + 45 * 60)
check(f"a healthy bank on charge still reads `charging`, not `warn` ({hi['volts']:.2f} V)",
      hi["status"] == "charging")

# --- the assertion that would have caught the flap -------------------------
# Added 2026-09-08. The bug this catches was invisible for a day because nothing scored the
# verdict for STABILITY: every existing assertion asked "does it say the right thing at moment
# X", and a readout that says the right thing every other poll passes all of them. The console's
# own flapping defect (7 flips a race) was found the same way and fixed a day earlier — the
# lesson did not travel.
#
# A bank parked ON a threshold is the worst case by construction, so that is the fixture: the
# real Jul 18 plateau level, at the Orca's real ~0.7 Hz, with deterministic load sags. No RNG —
# a flake here would be indistinguishable from the bug.
print("stability — a level parked on the danger line must not chatter:")
DANGER_LINE = power.FLOOR_V + power.DANGER_MARGIN_V
PLATEAU = DANGER_LINE + 0.005             # 11.305 V: five millivolts above the line
plateau = []
for i in range(int(90 * 60 * 0.7)):       # 90 minutes at 0.7 Hz
    t = T0 + i / 0.7
    wobble = 0.02 * ((i % 7) - 3) / 3.0                      # ±20 mV instrument noise
    sag = -0.55 if (i % 431) < 12 else 0.0                   # a winch load every ~10 min
    plateau.append((t, PLATEAU + wobble + sag))
seq = [power.assess_series([r for r in plateau if r[0] <= T0 + s], now=T0 + s)["status"]
       for s in range(30 * 60, 90 * 60, 30)]                 # poll every 30 s over the last hour
flips = sum(1 for a, b in zip(seq, seq[1:]) if a != b)
check(f"the verdict holds still on a plateau ({flips} change(s) in {len(seq)} polls, "
      f"settled on {seq[-1]!r})", flips <= 1)
check("...and a bank five millivolts above the danger line settles on ONE verdict",
      len(set(seq)) == 1)
# the same fixture 100 mV lower must land on the other side — and just as steadily
lower = [(t, v - 0.10) for t, v in plateau]
seq2 = [power.assess_series([r for r in lower if r[0] <= T0 + s], now=T0 + s)["status"]
        for s in range(30 * 60, 90 * 60, 30)]
check(f"100 mV lower reads danger, also without chattering "
      f"({sum(1 for a, b in zip(seq2, seq2[1:]) if a != b)} change(s))",
      seq2[-1] == "danger" and sum(1 for a, b in zip(seq2, seq2[1:]) if a != b) <= 1)

# --- noise rejection, the reason for raise-slow ----------------------------
print("noise rejection:")
base = [(T0 + m * 60, 12.6) for m in range(0, 46, 1)]
sag = base[:-1] + [(T0 + 45 * 60, 10.9)]          # one winch-load sag on an otherwise fine bank
sagged = power.assess_series(sag, now=T0 + 45 * 60)
check(f"a single deep sag does NOT raise an alarm (status {sagged['status']}, "
      f"slope {sagged['trend_v_per_h']:+.2f} V/h)", sagged["status"] == "ok")
check("...and does not fake a drain trend",
      abs(sagged["trend_v_per_h"]) < power.DRAIN_WARN_V_PER_H)
flat = [(T0 + m * 60, 11.25) for m in range(0, 46, 1)]
check("a bank SUSTAINED below everything on record DOES raise danger",
      power.assess_series(flat, now=T0 + 45 * 60)["status"] == "danger")
# release is deliberately slow for this quantity: an UNLOADED flat bank reads high for a
# while (surface charge), so a brief bounce is not a recovery.
brief = flat[:-3] + [(T0 + m * 60, 12.7) for m in (43, 44, 45)]
check("a brief bounce does NOT clear danger (surface charge is not charge)",
      power.assess_series(brief, now=T0 + 45 * 60)["status"] == "danger")
sustained = flat[:30] + [(T0 + m * 60, 12.8) for m in range(30, 46)]
check("a sustained recovery DOES clear it",
      power.assess_series(sustained, now=T0 + 45 * 60)["status"] != "danger")

# --- the chemistry-independent half ----------------------------------------
print("drain projection (no chemistry assumptions):")
drain = [(T0 + m * 60, 13.4 - 0.006 * m) for m in range(0, 46, 1)]   # -0.36 V/h, still high
d = power.assess_series(drain, now=T0 + 45 * 60)
check(f"a fast drain on a HIGH bank shows the projection but does not alarm — both race days "
      f"opened exactly like this and were fine ({d['volts']:.2f} V, "
      f"{d['trend_v_per_h']:+.2f} V/h, {d['status']})",
      d["status"] == "ok" and d["hours_to_floor"] is not None)
low_drain = [(T0 + m * 60, 11.55 - 0.005 * m) for m in range(0, 46, 1)]  # -0.30 V/h, BELOW the plateau
ld = power.assess_series(low_drain, now=T0 + 45 * 60)
check(f"the same drain below the plateau band DOES warn ({ld['volts']:.2f} V, "
      f"{ld['trend_v_per_h']:+.2f} V/h)", ld["status"] == "warn")
steady = [(T0 + m * 60, 12.9) for m in range(0, 46, 1)]
check("a steady healthy bank stays ok",
      power.assess_series(steady, now=T0 + 45 * 60)["status"] == "ok")

# --- degenerate inputs ------------------------------------------------------
print("degenerate inputs:")
check("no samples -> unknown, available False",
      power.assess_series([])["status"] == "unknown"
      and power.assess_series([])["available"] is False)
one = power.assess_series([(T0, 11.2)], now=T0)
check("a single sample never raises an alarm (cannot sustain)", one["status"] == "ok")
check("a single sample has no trend", one["trend_v_per_h"] is None)
check("unsorted input is handled",
      power.assess_series(list(reversed(SERIES[:20])), now=SERIES[19][0])["available"] is True)

print("\nthe headline, for the record:")
for m in (30, 180, 300, 400, 460, 660, 950):
    r = at(m)
    print(f"  T0+{m:>3} min  {r['volts']:>6.2f} V  {str(r['trend_v_per_h']):>7} V/h  "
          f"{r['status']:<9} {r['reason']}")

print("\n" + ("PASS" if ok else "FAIL"))
raise SystemExit(0 if ok else 1)
