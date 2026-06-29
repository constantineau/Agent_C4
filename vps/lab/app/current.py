"""Water currents (set & drift) for the optimizer — a CurrentField parallel to the WindField.

The boat sails at its polar speed THROUGH THE WATER; the current carries it over the GROUND. So the
isochrone advances each step by the boat's water-velocity PLUS the current's drift — the track then
bows with a cross stream, the boat crabs to hold its course made good, and ETAs reflect a fair vs foul
current. `current_at(lat, lon, epoch)` returns **(set_deg, drift_kn)** — the compass direction the
water is GOING (oceanographic convention) and its speed in knots; (0, 0) = no current.

v1 data source = NOAA GLOFS (Great Lakes Operational Forecast System) surface currents. The live GLOFS
NetCDF provider is wired in a follow-up (it needs netCDF4 + the LMHOFS/LHOFS NODD endpoint + nearest-
node sampling of the curvilinear/FVCOM grid); until then `build_currentfield` returns a ZeroCurrent so
currents are a safe no-op and the route is unchanged. The physics + integration are validated
deterministically with ConstantCurrent (see test_routing_currents.py).
"""


class CurrentField:
    """Base: no current. Subclasses override current_at."""
    loaded = False
    source = None

    def current_at(self, lat, lon, epoch):
        return (0.0, 0.0)

    def status(self):
        return {"loaded": self.loaded, "source": self.source}


class ZeroCurrent(CurrentField):
    """Explicit no-current field (routes exactly as before)."""
    pass


class ConstantCurrent(CurrentField):
    """A uniform current — for tests + manual what-ifs. set_deg = where the water flows TO."""
    loaded = True
    source = "constant"

    def __init__(self, set_deg, drift_kn):
        self.set_deg = float(set_deg)
        self.drift_kn = float(drift_kn)

    def current_at(self, lat, lon, epoch):
        return (self.set_deg, self.drift_kn)

    def status(self):
        return {"loaded": True, "source": "constant", "set_deg": self.set_deg, "drift_kn": self.drift_kn}


def build_currentfield(bbox, t_start, t_end, on_progress=None):
    """Best-effort GLOFS surface-current field over the course bbox + time window. Until the GLOFS
    NetCDF provider is wired this returns ZeroCurrent (currents off, route unchanged)."""
    log = on_progress or (lambda *_: None)
    log("currents: GLOFS provider not yet wired — routing without current")
    return ZeroCurrent()
