"""Sea state (significant wave height) for the optimizer — a WaveField parallel to the WindField and
CurrentField.

The ORC polar is a FLAT-WATER speed. Waves slow the boat below it — most upwind (slamming into a head
sea), least downwind (a following sea barely hurts, can even help) — so a route that looks fast on the
polar can be slower in a seaway, and the upwind/downwind legs degrade differently. Feeding sea state
in lets the optimizer route on ACHIEVABLE speed (with the boat's helm-skill factor), and the gap to the
theoretical polar becomes an honest coaching number. `wave_at(lat, lon, epoch) -> hs_m` = significant
wave height in metres; 0.0 = flat water / no data / out of domain.

Phase 1 (this file): the SEAM — `ZeroWave` (default, no behaviour change) + `ConstantWave` for tests
and a uniform what-if (`WAVES_CONST_HS`). Phase 2 wires a real Great-Lakes wave provider (NOAA GLWU
significant wave height via the CO-OPS THREDDS OPeNDAP server, mirroring the GLOFS current provider in
`current.py`). The degradation MODEL lives in `optimizer._wave_factor` so it's shared by any source.
"""
import os

ENABLED = os.environ.get("WAVES_ENABLED", "1").strip().lower() in ("1", "true", "yes", "on")
# A uniform sea state (m) for what-ifs / demos / tests when no real provider is wired — 0 = off.
CONST_HS = float(os.environ.get("WAVES_CONST_HS", "0"))


class WaveField:
    loaded = False
    source = None

    def wave_at(self, lat, lon, epoch):
        return 0.0

    def status(self):
        return {"loaded": self.loaded, "source": self.source}


class ZeroWave(WaveField):
    """Flat water everywhere — the default until a real provider is wired (route unchanged)."""
    pass


class ConstantWave(WaveField):
    """Uniform sea state — for tests + manual what-ifs (`WAVES_CONST_HS`)."""
    loaded = True
    source = "constant"

    def __init__(self, hs_m):
        self.hs_m = float(hs_m)

    def wave_at(self, lat, lon, epoch):
        return self.hs_m

    def status(self):
        return {"loaded": True, "source": "constant", "hs_m": self.hs_m}


def build_wavefield(bbox, t_start, t_end, on_progress=None):
    """Best-effort sea-state field over the course bbox + window. Phase 1: `ZeroWave` (or a uniform
    `ConstantWave` what-if via `WAVES_CONST_HS`). Phase 2 swaps in the real Great-Lakes wave provider;
    any miss → `ZeroWave` (route unchanged), like the GRIB/current layers."""
    log = on_progress or (lambda *_: None)
    if not ENABLED:
        return ZeroWave()
    if CONST_HS > 0:
        log(f"waves: uniform {CONST_HS} m sea state (what-if)")
        return ConstantWave(CONST_HS)
    return ZeroWave()
