"""shared.clock_trust: when is the Pi's wall clock trusted? (#184-9, rule settled in #240)

Trusted UTC comes from NTP (the kernel's sync flag) or a GNSS fix that agrees with the wall
clock; once trusted it stays trusted for the boot (marker file keyed by boot id).
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import clock_trust                                           # noqa: E402
from shared.clock_trust import ClockTrust                                # noqa: E402

ok = True


def check(name, cond):
    global ok
    ok = ok and bool(cond)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}")


WALL = 1793079200.0                     # 2026-10-27T05:33:20Z
FIX_OK = "2026-10-27T05:33:21.500Z"     # within the tolerance of WALL
FIX_FAR = "2026-07-18T18:00:00Z"        # a stale clock vs a live fix (or the reverse)
quiet = None


def clock(synced=False, marker=None, boot="boot-1"):
    return ClockTrust(marker, synced=lambda: synced, wall=lambda: WALL, boot=lambda: boot,
                      tolerance_s=2, log=quiet)


print("parse_utc:")
check("Z suffix", clock_trust.parse_utc("2026-10-27T05:33:20Z") == WALL)
check("garbage / naive / non-string -> None",
      clock_trust.parse_utc("nope") is None and clock_trust.parse_utc("2026-10-27T05:33:20")
      is None and clock_trust.parse_utc(5) is None)

print("NTP:")
check("not synced -> not trusted", clock().trusted is False)
c = clock(synced=True)
check("synced -> trusted (ntp)", c.trusted and c.source == "ntp")
check("unknown (None) -> not trusted", ClockTrust(None, synced=lambda: None, log=quiet).trusted
      is False)

print("GNSS:")
c = clock()
c.note_gnss(FIX_FAR)
check("a fix that disagrees with the wall clock does not latch", c.trusted is False)
c.note_gnss("garbage")
check("an unparseable fix does not latch", c.trusted is False)
c.note_gnss(FIX_OK)
check("a fix that agrees latches trust (gnss)", c.trusted and c.source == "gnss")

print("latch:")
flag = {"synced": True}
c = ClockTrust(None, synced=lambda: flag["synced"], wall=lambda: WALL, boot=lambda: "b",
               log=quiet)
check("trusted while synced", c.trusted)
flag["synced"] = False                  # NTP lost its servers offshore
check("stays trusted when the kernel later says unsynchronised", c.trusted)

with tempfile.TemporaryDirectory() as d:
    m = Path(d) / "clock-trusted"
    clock(synced=True, marker=m).trusted
    check("marker written with the boot id", m.read_text() == "boot-1")
    check("a restart in the same boot is trusted from the marker",
          clock(marker=m).trusted is True)
    check("a reboot (new boot id) starts untrusted", clock(marker=m, boot="boot-2").trusted
          is False)

print("kernel_synced() runs here:", clock_trust.kernel_synced())
print("ALL OK" if ok else "SOME CHECKS FAILED")
sys.exit(0 if ok else 1)
