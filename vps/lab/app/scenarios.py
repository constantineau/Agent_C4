"""Playbook v2 scenario REGISTRY + wind-field transforms (docs/PLAYBOOK_V2.md §3-4, §8).

Each external scenario is a cheap, deterministic TRANSFORM of the already-downloaded blended wind
field — rotation (the breeze goes right/left of forecast), TWS scaling (more/less pressure than
forecast), and time shift (the front arrives early/late) — so plays exist even when every model
agrees. `TransformedWind` wraps the base `WindField` and perturbs only what `detail_at` returns;
everything else (loaded/meta/series/bbox/status/sample_grid) passes through, so
`optimizer.optimize_course` routes a scenario exactly like the real field.

Scenario ORDERING is point-of-sail aware (locked Phase-B input #6: the retro study showed top
boats beat their own optimal by sailing hotter angles in PRESSURE — TWS scenarios outrank TWD
rotations for downwind-heavy races; upwind the rotations lead because side/shift leverage pays).

Detection-condition templates are resolved at synthesis time against the route context (mean TWS)
and the venue's fleet-normal stats (locked input #3: thresholds are percentile-framed, not
guessed) → the play's machine predicates; the crew-facing narrative is authored by the frontier
model in synthesis (deterministic fallback text here).
"""


class TransformedWind:
    """A perturbed view of a WindField. rot_deg rotates TWD (+ = right/clockwise), tws_scale
    multiplies speed, time_shift_h samples the field EARLY (+3 = the weather forecast for t+3h
    arrives at t — 'the front is early')."""

    def __init__(self, base, rot_deg=0.0, tws_scale=1.0, time_shift_h=0.0):
        self.base = base
        self.rot_deg = float(rot_deg)
        self.tws_scale = float(tws_scale)
        self.shift_s = float(time_shift_h) * 3600.0

    def detail_at(self, lat, lon, epoch):
        d = self.base.detail_at(lat, lon, epoch + self.shift_s)
        if not d:
            return None
        d = dict(d)
        d["tws"] = round(d["tws"] * self.tws_scale, 2)
        d["twd"] = round((d["twd"] + self.rot_deg) % 360.0, 1)
        return d

    def wind_at(self, lat, lon, epoch):
        d = self.detail_at(lat, lon, epoch)
        return (d["tws"], d["twd"]) if d else None

    def __getattr__(self, name):          # loaded / meta / series / bbox / t_* / status / sample_grid
        return getattr(self.base, name)


def _rot_predicates(deg, ctx):
    """A persistent shift of roughly this magnitude vs the frozen forecast. Threshold at ~60% of
    the scenario's rotation (arm on the way TO the scenario, not after it fully lands)."""
    thr = max(8, round(abs(deg) * 0.6))
    sign = 1 if deg > 0 else -1
    return [{"signal": "drift_twd_signed_deg", "op": ">=" if sign > 0 else "<=",
             "value": sign * thr, "sustain_min": 20},
            {"signal": "shift_persistent", "op": "==", "value": True}]


def _tws_predicates(scale, ctx):
    """Pressure above/below forecast: threshold = ~60% of the scenario's TWS delta at the route's
    mean wind (a ×1.25 scenario on a 12 kn forecast arms at +1.8 kn sustained)."""
    mean = float(ctx.get("mean_tws") or 10.0)
    delta = (scale - 1.0) * mean
    thr = round(abs(delta) * 0.6, 1) or 1.0
    return [{"signal": "drift_tws_kn", "op": ">=" if delta > 0 else "<=",
             "value": thr if delta > 0 else -thr, "sustain_min": 20}]


def _timing_predicates(hours, ctx):
    """The forecast evolution is running early/late — detected as sustained drift whose SIGN flips
    with where the boat is relative to the frozen timeline; the narrative (LLM side) carries the
    nuance, the predicate arms on material sustained drift either way."""
    return [{"signal": "drift_twd_deg", "op": ">=", "value": 15, "sustain_min": 30}]


def _wave_predicates(_factor, ctx):
    return [{"signal": "polar_pct", "op": "<=", "value": 88, "sustain_min": 30}]


EXTERNAL = [
    # kind/params → the transform; detect → predicate template resolved at synthesis time
    {"id": "shift_right_10", "name": "Breeze 10° right of forecast", "kind": "rotation",
     "params": {"deg": 10}, "detect": _rot_predicates,
     "narrative": "The breeze has gone right of the frozen forecast and held — a moderate persistent right shift, not an oscillation."},
    {"id": "shift_left_10", "name": "Breeze 10° left of forecast", "kind": "rotation",
     "params": {"deg": -10}, "detect": _rot_predicates,
     "narrative": "The breeze has gone left of the frozen forecast and held — a moderate persistent left shift, not an oscillation."},
    {"id": "shift_right_20", "name": "Breeze 20°+ right of forecast", "kind": "rotation",
     "params": {"deg": 20}, "detect": _rot_predicates,
     "narrative": "A large persistent right shift vs the frozen forecast — the wind the plan assumed is not the wind you have."},
    {"id": "shift_left_20", "name": "Breeze 20°+ left of forecast", "kind": "rotation",
     "params": {"deg": -20}, "detect": _rot_predicates,
     "narrative": "A large persistent left shift vs the frozen forecast — the wind the plan assumed is not the wind you have."},
    {"id": "pressure_up", "name": "More pressure than forecast", "kind": "tws_scale",
     "params": {"scale": 1.25}, "detect": _tws_predicates,
     "narrative": "Sustained wind speed well above the frozen forecast — pressure the plan didn't count on; hotter angles and earlier change-downs may pay."},
    {"id": "pressure_down", "name": "Less pressure than forecast", "kind": "tws_scale",
     "params": {"scale": 0.75}, "detect": _tws_predicates,
     "narrative": "Sustained wind speed well below the frozen forecast — a lighter race than planned; the light-air sail plan and the pace expectations both move."},
    {"id": "front_early", "name": "System arrives early", "kind": "time_shift",
     "params": {"hours": 3}, "detect": _timing_predicates,
     "narrative": "The forecast evolution is running AHEAD of schedule — features (shift/front/fill) arriving hours early; downstream legs meet different wind than planned."},
    {"id": "front_late", "name": "System arrives late", "kind": "time_shift",
     "params": {"hours": -3}, "detect": _timing_predicates,
     "narrative": "The forecast evolution is running BEHIND schedule — the expected change hasn't come; holding the pre-change mode longer may pay."},
    {"id": "sea_state_up", "name": "Rougher than forecast", "kind": "wave_heavy",
     "params": {"factor": 2.0}, "detect": _wave_predicates,
     "narrative": "Sea state materially worse than the plan assumed — upwind speed suffers most; the flatter-water side of the course gains value."},
]


def apply(scenario, wf):
    """The transformed field for a scenario (wave_heavy routes on the SAME wind — its perturbation
    is an optimizer wave-coefficient override, returned as the second element)."""
    k, p = scenario["kind"], scenario["params"]
    if k == "rotation":
        return TransformedWind(wf, rot_deg=p["deg"]), None
    if k == "tws_scale":
        return TransformedWind(wf, tws_scale=p["scale"]), None
    if k == "time_shift":
        return TransformedWind(wf, time_shift_h=p["hours"]), None
    if k == "wave_heavy":
        return wf, {"wave_scale": p["factor"]}
    return wf, None


def pos_profile(nominal_result):
    """Fraction of the nominal route's time spent upwind / reaching / downwind (from its legs)."""
    tot, acc = 0.0, {"upwind": 0.0, "reaching": 0.0, "downwind": 0.0}
    for leg in (nominal_result or {}).get("legs") or []:
        m = float(leg.get("leg_minutes") or 0)
        pos = str(leg.get("point_of_sail") or "reaching")
        acc[pos if pos in acc else "reaching"] += m
        tot += m
    if not tot:
        return {"upwind": 0.34, "reaching": 0.33, "downwind": 0.33}
    return {k: round(v / tot, 2) for k, v in acc.items()}


def select(profile, max_n=None):
    """The registry ordered for THIS race (locked Phase-B input #6): downwind-heavy → pressure
    (TWS) scenarios first, rotations demoted; upwind-heavy → rotations first. Timing + sea state
    keep mid priority. Deterministic order, stable ids."""
    downwind = (profile or {}).get("downwind", 0.33) >= 0.45

    def key(s):
        base = {"tws_scale": 0, "rotation": 1, "time_shift": 2, "wave_heavy": 3} if downwind \
            else {"rotation": 0, "tws_scale": 1, "time_shift": 2, "wave_heavy": 3}
        return (base.get(s["kind"], 9), abs(next(iter(s["params"].values()), 0)))

    out = sorted(EXTERNAL, key=key)
    return out[:max_n] if max_n else out
