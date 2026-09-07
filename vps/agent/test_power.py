"""Bank watch — would the system have warned before the Jul 18 archive died?

The fixture below is the REAL house-bank voltage from the race, 5-minute means straight out of
`telemetry_raw` (`electrical.batteries.0.voltage`, the Orca Core), 13:00Z Jul 18 -> 06:00Z
Jul 19. It is the whole point of the module: at 20:40:30Z (T0+460 min) the full-res archiver
and the AIS transceiver both stopped at the bottom of this curve, the boat retired, and nothing
in the system had said a word — the path was in no PRESENT table, no alert rule and no tile.

So the load-bearing assertions are temporal: warn well before the failure, danger before it,
and no alarm during the motor home when the alternator is putting the bank back.

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

warn_at = first_status("warn")
check(f"warns at T0+{warn_at} min ({'—' if warn_at is None else at(warn_at)['volts']} V), "
      f"before the archiver died at T0+{ARCHIVE_DIED:.0f}",
      warn_at is not None and warn_at < ARCHIVE_DIED)
check(f"...and with >1 h of warning ({'' if warn_at is None else ARCHIVE_DIED - warn_at:.0f} min)",
      warn_at is not None and ARCHIVE_DIED - warn_at > 60)

danger_at = first_status("danger")
check(f"escalates to danger at T0+{danger_at} min, still before the failure",
      danger_at is not None and danger_at < ARCHIVE_DIED)
check("danger comes after warn (it escalates, never skips)",
      danger_at is not None and warn_at is not None and danger_at >= warn_at)

died = at(ARCHIVE_DIED)
check(f"at the moment of failure it reads danger ({died['volts']:.2f} V)",
      died["status"] == "danger")
# while the bank is genuinely falling, the projection must name a time
falling = at(180)
check(f"the projection names a time the instruments are at risk "
      f"(~{falling['hours_to_floor']} h at {falling['trend_v_per_h']:+.2f} V/h)",
      falling["hours_to_floor"] is not None and falling["dark_at_epoch"] is not None)

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
flat_charging = [(T0 + m * 60, 11.20 + 0.005 * m) for m in range(0, 46)]   # +0.3 V/h at 11.2 V
fc = power.assess_series(flat_charging, now=T0 + 45 * 60)
check(f"a flat-but-recovering bank still reports danger ({fc['volts']:.2f} V, "
      f"{fc['trend_v_per_h']:+.2f} V/h)", fc["status"] == "danger" and fc["charging"] is True)

# --- noise rejection, the reason for raise-slow ----------------------------
print("noise rejection:")
base = [(T0 + m * 60, 12.6) for m in range(0, 46, 1)]
sag = base[:-1] + [(T0 + 45 * 60, 10.9)]          # one winch-load sag on an otherwise fine bank
sagged = power.assess_series(sag, now=T0 + 45 * 60)
check(f"a single deep sag does NOT raise an alarm (status {sagged['status']}, "
      f"slope {sagged['trend_v_per_h']:+.2f} V/h)", sagged["status"] == "ok")
check("...and does not fake a drain trend",
      abs(sagged["trend_v_per_h"]) < power.DRAIN_WARN_V_PER_H)
flat = [(T0 + m * 60, 11.4) for m in range(0, 46, 1)]
check("a sustained low bank DOES raise danger",
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
check(f"a fast drain warns even above the level thresholds ({d['volts']:.2f} V, "
      f"{d['trend_v_per_h']:+.2f} V/h)", d["status"] == "warn" and d["volts"] > power.WARN_V)
check("...and projects the time to the floor", d["hours_to_floor"] is not None)
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
