"""Can this box's wall clock be trusted yet? (#184-9, settled in #240.)

The Pi has no RTC. After a power-up its clock starts wherever fake-hwclock (or nothing) left it
until NTP or a GNSS fix corrects it, so anything stamped in that stretch can carry a stale date.
The owner's rule (#240, "Pi clock before sync"):

  - trusted UTC comes from valid GNSS or NTP;
  - collection continues before that, but pruning and timestamped publishing wait until the
    time is trusted;
  - intervals use the monotonic clock.

`ClockTrust.trusted` answers the first part. It latches: once the clock has been set in this
boot it stays right (it only drifts), and an NTP daemon that loses its servers offshore will
eventually mark the kernel clock "unsynchronised" again — that must not stop pruning or the
uplink mid-race. The latch is also written to a small marker file keyed by the kernel boot id,
so a container restart in the same boot does not have to wait for a fresh fix; a reboot (new
boot id) starts untrusted again, which is exactly the case the rule is about.

Two ways in:
  - NTP: the kernel's own sync flag, read with adjtimex(2) in read-only mode (modes = 0, allowed
    without CAP_SYS_TIME and by Docker's default seccomp profile). chrony and systemd-timesyncd
    both clear STA_UNSYNC once they have set the clock; adjtimex then stops returning TIME_ERROR.
  - GNSS: Signal K's `navigation.datetime` (from the GPS's PGN 126992/129029). A fix is taken
    as proof only when it AGREES with the wall clock within CLOCK_GNSS_TOLERANCE_S — a stale
    clock disagrees with a live fix, and a stale replayed value disagrees with a correct clock,
    so neither can latch trust by mistake. (Nothing here sets the clock; that is the OS's job.)

Pure stdlib: shared by the archiver and the uplink.
"""
import ctypes
import os
import time
from datetime import datetime
from pathlib import Path

GNSS_PATH = "navigation.datetime"
GNSS_TOLERANCE_S = float(os.environ.get("CLOCK_GNSS_TOLERANCE_S", "2"))
_TIME_ERROR = 5                 # adjtimex(2): clock not synchronised (STA_UNSYNC set)
_BOOT_ID = "/proc/sys/kernel/random/boot_id"


def kernel_synced():
    """True when the kernel says an NTP daemon has synchronised the clock, False when it says
    it has not, None when it cannot be asked (no libc / not Linux)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        buf = ctypes.create_string_buffer(512)   # struct timex is 208 bytes on 64-bit; modes=0
        state = libc.adjtimex(buf)
    except (OSError, AttributeError):
        return None
    if state < 0:
        return None
    return state != _TIME_ERROR


def boot_id():
    """The kernel's id for this boot (shared by every container on the box), or ""."""
    try:
        return Path(_BOOT_ID).read_text().strip()
    except OSError:
        return ""


def parse_utc(value):
    """Epoch seconds from an ISO-8601 UTC string (Signal K style), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


class ClockTrust:
    """`trusted` = the wall clock is right (latched). `note_gnss(value)` offers a GNSS fix."""

    def __init__(self, marker=None, synced=kernel_synced, wall=time.time, boot=boot_id,
                 tolerance_s=None, log=print):
        self.marker = Path(marker) if marker else None
        self._synced = synced
        self._wall = wall
        self._boot = boot
        self.tolerance_s = GNSS_TOLERANCE_S if tolerance_s is None else float(tolerance_s)
        self._log = log
        self.source = None          # "ntp" | "gnss" | "earlier in this boot" once trusted

    @property
    def trusted(self):
        if self.source:
            return True
        if self._marker_matches():
            self.source = "earlier in this boot"
            return True
        if self._synced() is True:
            self._latch("ntp")
        return bool(self.source)

    def note_gnss(self, value):
        """A GNSS date/time just received. Latches trust when it agrees with the wall clock."""
        if self.source:
            return
        t = parse_utc(value)
        if t is not None and abs(t - self._wall()) <= self.tolerance_s:
            self._latch("gnss")

    def _marker_matches(self):
        if self.marker is None:
            return False
        b = self._boot()
        try:
            return bool(b) and self.marker.read_text().strip() == b
        except OSError:
            return False

    def _latch(self, source):
        self.source = source
        if self._log:
            self._log(f"[clock] wall clock trusted ({source})", flush=True)
        b = self._boot()
        if self.marker is not None and b:
            try:
                tmp = self.marker.with_name(self.marker.name + ".tmp")
                tmp.write_text(b)
                os.replace(tmp, self.marker)
            except OSError:
                pass                # best effort: it just has to be re-proven after a restart
