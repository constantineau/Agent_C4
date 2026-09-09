#!/usr/bin/env python3
"""The boat's ACTUAL polar — observed STW per measured (TWS, TWA) cell. Instruments only.

Cole, 2026-09-09: "just compute the polars off of the instruments — not off of
theoretical/forecast data." So this is NOT a %-of-certificate report. It bins the boat's own
record — measured TWS, measured TWA, measured STW, every one ranked per second aboard — on a
plain regular grid (2 kn × 10°), and the polar is the 80th-percentile STW per cell: best
achievable, lulls and steering scatter rejected. The ORC cert appears only as a reference
column where it happens to rate a nearby cell; cells the cert has no opinion on (the crew's
A3+SS, S2+SS combos; angles outside the rated range) stand on the same footing as everything
else, because the record is the authority here.

Upstream guards still apply: the race window is derived from the record, the trust gate refuses
danger windows (Jul 18's −98° compass), the kite gate marks impossible configs unattributed,
and sessions whose motion came from a bench source are skipped and said.

Run inside the lab image with the repo mounted:
  docker run --rm --network sr33-dev_default \\
    -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \\
    -v $PWD/tools/analysis/observed_polar.py:/srv/observed_polar.py:ro \\
    -v /home/constantineau/backups/race-data:/out \\
    -e AGENT_URL=http://<agent>:8000 -e BOAT_PASSWORD=... \\
    -w /srv sr33-dev-lab python observed_polar.py --out /out/observed-polar.json
"""
import argparse
import json
import time

from app import monitor
from app import polars as POL
from app import track as T

TWS_STEP_KN = 2.0
TWA_STEP_DEG = 10.0
MIN_SAMPLES = 30          # seconds of evidence a cell needs before it is a polar point
PCTILE = 80.0


def gate(fixes, trust):
    dz = [(a, b) for iv in (trust.get("danger") or {}).values() for a, b in (iv or ())]
    if not dz:
        return fixes, 0
    kept = [f for f in fixes if not any(a <= f["t"] <= b for a, b in dz)]
    return kept, len(fixes) - len(kept)


def cert_ref(P, tws, twa):
    """The cert target for the NEAREST rated cell, reference only — None when nothing is near."""
    if not P:
        return None
    c = min(P, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))
    return round(c[2], 2) if abs(c[0] - tws) <= 2.0 and abs(c[1] - twa) <= 10.0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-points", type=int, default=20000)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    a = ap.parse_args()

    P = POL.polars_stw()
    cells = {}            # (tws_bucket, twa_bucket, config) -> [stw, ...]
    races = {}
    for s in monitor.agent_json("/racelog/sessions")["sessions"]:
        w = s.get("window") or {}
        if not w.get("hours"):
            continue
        if any("unverified" in p for p in (w.get("provenance") or ())):
            print(f"skip {s['name']}: window derived from a source that is not this boat")
            continue
        r = monitor.agent_json(f"/racelog/track?start={w['start_ts']}&end={w['end_ts']}"
                               f"&max_points={a.max_points}")
        trust = monitor.agent_json(f"/racelog/trust?start={w['start_ts']}&end={w['end_ts']}")
        fixes, refused = gate(r["fixes"], trust)
        sail_log = r.get("sail_log") or []
        n = 0
        for f in fixes:
            tws, twa, stw = f.get("tws"), f.get("twa"), f.get("stw")
            if tws is None or twa is None or stw is None:     # instruments or nothing
                continue
            if tws < 1.0 or stw <= 0.3:
                continue
            twa = abs(twa)
            if twa > 180.0:
                twa = 360.0 - twa
            cfg = T.config_at(sail_log, f["t"])
            if not T._config_plausible(cfg, twa):
                cfg = None                                    # a douse nobody tapped
            key = (round(tws / TWS_STEP_KN) * TWS_STEP_KN,
                   round(twa / TWA_STEP_DEG) * TWA_STEP_DEG, cfg)
            cells.setdefault(key, []).append(stw)
            races.setdefault(key, set()).add(s["name"])
            n += 1
        print(f"{s['name']}: {len(r['fixes'])} fixes, {refused} refused by trust, "
              f"{n} measured samples — {trust.get('summary', {}).get('line')}")

    rows = []
    for (tws, twa, cfg), stws in cells.items():
        if len(stws) < a.min_samples:
            continue
        stws.sort()
        obs = stws[min(len(stws) - 1, int(len(stws) * PCTILE / 100.0))]
        rows.append({"tws": tws, "twa": twa, "config": cfg,
                     "stw": round(obs, 2), "median_stw": round(stws[len(stws) // 2], 2),
                     "samples": len(stws), "races": sorted(races[(tws, twa, cfg)]),
                     "cert_ref": cert_ref(P, tws, twa)})
    rows.sort(key=lambda r: (r["tws"], r["twa"], r["config"] or ""))

    print(f"\nACTUAL POLAR — {len(rows)} cells with >= {a.min_samples}s of measured evidence "
          f"(STW p{PCTILE:.0f}, instruments only)")
    print(f"{'TWS':>4} {'TWA':>5} {'config':10} {'STW':>6} {'med':>6} {'n(s)':>6} "
          f"{'cert*':>6}  races")
    for r in rows:
        ref = f"{r['cert_ref']:.2f}" if r["cert_ref"] is not None else "  —"
        print(f"{r['tws']:>4.0f} {r['twa']:>5.0f} {str(r['config'] or '—'):10} "
              f"{r['stw']:>6.2f} {r['median_stw']:>6.2f} {r['samples']:>6} {ref:>6}  "
              f"{len(r['races'])}")
    print("(* nearest ORC-rated cell, reference only — the polar above is the record)")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"generated_at": time.time(), "wind_source": "measured",
                       "grid": {"tws_step_kn": TWS_STEP_KN, "twa_step_deg": TWA_STEP_DEG,
                                "pctile": PCTILE, "min_samples": a.min_samples},
                       "note": "observed STW per measured (TWS,TWA[,config]) cell — "
                               "instruments only, trust-gated, kite-gated",
                       "cells": rows}, fh, indent=1)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
