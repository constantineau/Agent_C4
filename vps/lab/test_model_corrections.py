"""#384: manual per-model corrections in the wind field — rotate, scale, time shift, fade.

No network: the frames are built in memory.
"""
import math

import numpy as np
import pytest

from app.wind import corrections as C
from app.wind import grib
from app.wind.windfield import WindField

T0 = 1_800_000_000.0
H = 3600.0
LAT = np.arange(43.0, 45.01, 0.25)
LON = np.arange(-83.5, -80.49, 0.25)
BBOX = (45.0, 43.0, -83.5, -80.5)


def _uv(tws_kn, twd):
    s = tws_kn / grib.KN_PER_MS
    r = math.radians(twd)
    return -s * math.sin(r), -s * math.cos(r)


def series(model, hours, tws_of_k, twd_of_k):
    out = []
    for k in range(hours + 1):
        u, v = _uv(tws_of_k(k), twd_of_k(k))
        out.append(grib.GribFrame(model, "det", T0 + k * H, LAT, LON,
                                  np.full((LAT.size, LON.size), u),
                                  np.full((LAT.size, LON.size), v), True))
    return out


def field(corrections=None):
    # GFS: speed and direction change every hour so a time shift is visible
    s = {("gfs", "det"): series("gfs", 24, lambda k: 8.0 + k * 0.5, lambda k: (200 + 5 * k) % 360)}
    return WindField(s, [], BBOX, T0, T0 + 24 * H, corrections=corrections)


def angdiff(a, b):
    return (a - b + 180.0) % 360.0 - 180.0


def test_rotate_10_turns_every_direction_by_10():
    raw, cor = field(), field({"gfs": {"rotate_deg": 10}})
    for k in range(0, 24, 2):
        for lat, lon in [(43.5, -82.0), (44.6, -81.1)]:
            t = T0 + k * H + 900
            a, b = raw.wind_at(lat, lon, t), cor.wind_at(lat, lon, t)
            assert abs(angdiff(b[1], a[1]) - 10.0) < 0.15      # + = shifted right (clockwise)
            assert abs(b[0] - a[0]) < 0.02                     # speed untouched
    left = field({"gfs": {"rotate_deg": -10}})
    a, b = raw.wind_at(44, -82, T0 + 3 * H), left.wind_at(44, -82, T0 + 3 * H)
    assert abs(angdiff(b[1], a[1]) + 10.0) < 0.15


def test_scale_80_scales_speed():
    raw, cor = field(), field({"gfs": {"scale_pct": 80}})
    for k in range(0, 24, 3):
        t = T0 + k * H
        a, b = raw.wind_at(44, -82, t), cor.wind_at(44, -82, t)
        assert abs(b[0] - 0.8 * a[0]) < 0.02
        assert abs(angdiff(b[1], a[1])) < 0.15


def test_time_shift_plus_1h_samples_one_hour_earlier():
    raw, cor = field(), field({"gfs": {"shift_h": 1}})
    for k in range(2, 22):
        t = T0 + k * H
        assert cor.wind_at(44, -82, t) == raw.wind_at(44, -82, t - H)
    early = field({"gfs": {"shift_h": -2}})
    assert early.wind_at(44, -82, T0 + 5 * H) == raw.wind_at(44, -82, T0 + 7 * H)


def test_fade_is_linear_to_zero_at_n_minutes():
    t0 = T0 + 4 * H
    c = C.normalize({"gfs": {"rotate_deg": 20, "fade_min": 120, "fade_from": t0}})["gfs"]
    assert C.weight(c, t0 - H) == 1.0
    assert C.weight(c, t0) == 1.0
    assert C.weight(c, t0 + 30 * 60) == pytest.approx(0.75)
    assert C.weight(c, t0 + 60 * 60) == pytest.approx(0.5)
    assert C.weight(c, t0 + 120 * 60) == 0.0
    assert C.weight(c, t0 + 10 * H) == 0.0
    raw, cor = field(), field({"gfs": c})
    for mins, want in [(-60, 20.0), (0, 20.0), (30, 15.0), (60, 10.0), (90, 5.0), (120, 0.0),
                       (300, 0.0)]:
        t = t0 + mins * 60
        got = angdiff(cor.wind_at(44, -82, t)[1], raw.wind_at(44, -82, t)[1])
        assert abs(got - want) < 0.15, (mins, got)


def test_fade_without_a_time_starts_at_the_route_start():
    c = C.normalize({"gfs": {"scale_pct": 50, "fade_min": 60}})["gfs"]
    raw, cor = field(), field({"gfs": c})
    a, b = raw.wind_at(44, -82, T0 + 30 * 60), cor.wind_at(44, -82, T0 + 30 * 60)
    assert abs(b[0] - 0.75 * a[0]) < 0.02
    assert cor.wind_at(44, -82, T0 + 2 * H) == raw.wind_at(44, -82, T0 + 2 * H)


def test_only_the_corrected_model_changes():
    s = {("gfs", "det"): series("gfs", 12, lambda k: 10.0, lambda k: 270.0),
         ("hrrr", "det"): series("hrrr", 12, lambda k: 10.0, lambda k: 270.0)}
    raw = WindField(s, [], BBOX, T0, T0 + 12 * H)
    cor = WindField(s, [], BBOX, T0, T0 + 12 * H, corrections={"hrrr": {"rotate_deg": 30}})
    d = cor.detail_at(44, -82, T0 + H)
    assert d["twd_spread_deg"] > 5                       # the models now disagree
    assert 270 < d["twd"] < 300
    assert raw.detail_at(44, -82, T0 + H)["twd_spread_deg"] < 0.5


def test_normalize_and_text():
    assert C.normalize(None) == {}
    assert C.normalize({"gfs": {"rotate_deg": 0, "scale_pct": 100, "shift_h": 0}}) == {}
    n = C.normalize([{"model": "GFS", "rotate_deg": "-10", "scale_pct": 80, "shift_h": 1.5}])
    assert n == {"gfs": {"rotate_deg": -10.0, "scale_pct": 80.0, "shift_h": 1.5,
                         "fade_min": None, "fade_from": None}}
    assert C.normalize({"gfs": {"rotate_deg": 500}})["gfs"]["rotate_deg"] == C.ROTATE_MAX
    assert C.describe(n) == ["GFS: 10° left, speed 80%, 1.5 h later"]
    s = C.summary(n, used_models=["hrrr"])
    assert s["applied"] is False and s["items"][0]["unused"]
    s = C.summary({"gfs": dict(n["gfs"], fade_min=60.0)}, used_models=["gfs"])
    assert s["applied"] and s["text"] == ["GFS: 10° left, speed 80%, 1.5 h later, "
                                          "back to raw over 60 min from start"]
    assert C.summary({}) is None


def test_status_reports_corrections():
    st = field({"gfs": {"rotate_deg": 10}}).status()
    assert st["corrections"]["text"] == ["GFS: 10° right"]
    assert "corrections" not in field().status()


def test_pdf_says_the_forecast_was_corrected():
    import io

    import pypdf

    from app import report

    def text(corr):
        r = {"legs": [{"to": "Finish", "leg_minutes": 60, "wind": {"tws": 10, "twd": 200}}],
             "total_hours": 1.0, "corrections": corr}
        pdf = report.build_gameplan_pdf({"result": r, "race_name": "Test race"})
        return " ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages)

    corr = C.summary(C.normalize({"gfs": {"rotate_deg": 10, "scale_pct": 80}}), ["gfs"])
    assert "Corrected forecast: GFS: 10° right, speed 80%" in " ".join(text(corr).split())
    assert "Corrected forecast" not in text(None)


class _RecordingSource:
    """A duck-typed API-grid source that records the window it was asked to load."""
    kind, priority = "deterministic", 1.0

    def __init__(self, name):
        self.name, self.asked = name, None

    def load_series(self, bbox, t0, t1, on_progress=None):
        self.asked = (t0, t1)
        frames = series(self.name, int((t1 - t0) / H), lambda k: 10.0, lambda k: 270.0)
        for i, fr in enumerate(frames):
            fr.valid_time = t0 + i * H
        return {(self.name, "det"): frames}, {"model": self.name, "frames": len(frames)}


def test_a_shifted_model_is_loaded_over_the_shifted_window():
    from app.wind.windfield import build_windfield
    late, early, plain = _RecordingSource("gfs"), _RecordingSource("hrrr"), _RecordingSource("icon")
    wf = build_windfield(BBOX, T0, T0 + 12 * H, models=[late, early, plain],
                         corrections={"gfs": {"shift_h": 6, "fade_min": 60},
                                      "hrrr": {"shift_h": -3}})
    assert late.asked == (T0 - 6 * H, T0 + 12 * H)        # +6 h reads 6 h before the start
    assert early.asked == (T0, T0 + 15 * H)               # −3 h reads 3 h past the end
    assert plain.asked == (T0, T0 + 12 * H)
    assert wf.t_start == T0                                # the fade still counts from the start
    d = wf.detail_at(44, -82, T0)
    assert d and not d.get("held") and d["models"] == ["gfs", "hrrr", "icon"]


def test_a_fade_start_that_is_not_a_time_falls_back_to_the_route_start():
    for bad in (1e100, -5.0, float("inf"), "soon"):
        c = C.normalize({"gfs": {"rotate_deg": 10, "fade_min": 30, "fade_from": bad}})["gfs"]
        assert c["fade_from"] is None
        assert C.describe({"gfs": c}) == ["GFS: 10° right, back to raw over 30 min from start"]
