#!/usr/bin/env python3
"""The boat's ACTUAL polar, from the shore — a thin CLI over `app/obspolar.py`.

The engine lives in the Lab (`vps/lab/app/obspolar.py`) and serves the Polar tab; this wrapper
exists so the same build can be run and inspected from a shell. One implementation on purpose —
this repo has already paid for running analyses from two copies of a thing (the sail-log cursor,
the bench boat_id).

Run inside the lab image with the repo mounted:
  docker run --rm --network sr33-dev_default \\
    -v $PWD/vps/lab/app:/srv/app:ro -v $PWD/shared:/srv/shared:ro \\
    -v $PWD/tools/analysis/observed_polar.py:/srv/observed_polar.py:ro \\
    -v /home/constantineau/backups/race-data:/out \\
    -e AGENT_URL=http://<agent>:8000 -e BOAT_PASSWORD=... -e MONITOR_TIMEOUT_S=120 \\
    -w /srv sr33-dev-lab python observed_polar.py --out /out/observed-polar.json
"""
import argparse
import json

from app import obspolar
from app import polars as POL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    art = obspolar.compute()
    P = POL.polars_stw()

    def cert_ref(tws, twa):
        if not P:
            return None
        c = min(P, key=lambda p: abs(p[0] - tws) + abs(p[1] - twa))
        return round(c[2], 2) if abs(c[0] - tws) <= 2.0 and abs(c[1] - twa) <= 10.0 else None

    for r in art["races"]:
        print(f"{r['name']}: " + (r.get("skipped") or
              f"{r['fixes']} fixes, {r['refused_by_trust']} refused by trust, "
              f"{r['measured_samples']} measured samples — {r.get('trust_line')}"))
    rows = art["cells"]
    print(f"\nACTUAL POLAR — {len(rows)} cells "
          f"(p{art['grid']['pctile']:.0f} STW, >= {art['grid']['min_samples']}s each, "
          f"instruments only)")
    print(f"{'TWS':>4} {'TWA':>5} {'config':10} {'STW':>6} {'med':>6} {'n(s)':>6} {'cert*':>6}  races")
    for r in rows:
        ref = cert_ref(r["tws"], r["twa"])
        print(f"{r['tws']:>4.0f} {r['twa']:>5.0f} {str(r['config'] or '—'):10} "
              f"{r['stw']:>6.2f} {r['median_stw']:>6.2f} {r['samples']:>6} "
              f"{(f'{ref:.2f}' if ref is not None else '  —'):>6}  {len(r['races'])}")
    print("(* nearest ORC-rated cell, reference only — the polar above is the record)")
    if a.out:
        with open(a.out, "w") as fh:
            json.dump(art, fh, indent=1)
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
