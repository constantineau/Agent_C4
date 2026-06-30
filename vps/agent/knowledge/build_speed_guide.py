#!/usr/bin/env python3
"""Distill the ORC certificate export (C4_boatspeed_gospel.md) into clean artifacts.

The raw export (data.orc.org Speed Guide for SR33 "C4", CAN100) contains five sets of
per-TWS tables: a "Best Performance" envelope plus one per sail in the inventory
(Headsail J1-A, Symmetric S2-A, Asymmetric A2-A/A3-A). Each table has columns
TWA, BTV (target boatspeed), VMG, AWS, AWA, Heel, Reef, Flat.

This script emits:
  1. sr33_speed_guide.md  — clean reference loaded into the agent's standing context. Adds,
     per Best-Performance row, the OPTIMAL SAIL (derived by matching the per-sail polars) so
     the agent can advise sail selection and call crossovers / sail changes.
  2. ../../db/seed/polars_sr33.sql — real polars (target_stw=BTV, target_vmg=VMG).

Re-run if the ORC certificate is updated:  python3 vps/agent/knowledge/build_speed_guide.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "C4_boatspeed_gospel.md")
GUIDE = os.path.join(HERE, "sr33_speed_guide.md")
SEED = os.path.abspath(os.path.join(HERE, "..", "..", "db", "seed", "polars_sr33.sql"))
CROSSOVERS = os.path.abspath(os.path.join(HERE, "..", "..", "db", "seed", "sr33_crossovers.json"))
SAIL_POLARS = os.path.abspath(os.path.join(HERE, "..", "..", "db", "seed", "sr33_sail_polars.json"))

NUM = lambda s: float(s.replace("°", "").strip())

# Sail id -> crew-friendly label.
SAIL_NAMES = {
    "J1-A": "Headsail/jib (J1)",
    "S2-A": "Symmetric kite (S2)",
    "A2-A": "Asym A2 (76%)",
    "A3-A": "Asym A3",
}


def parse_groups(text):
    """Return {group_key: {"label": str, "blocks": {tws: [rows]}}} for the data section."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if "**Best Performance**" in l)
    groups, key, tws = {}, None, None
    for l in lines[start:]:
        s = l.strip()
        if "**Best Performance**" in s:
            key, tws = "best", None
            groups[key] = {"label": "Best Performance", "blocks": {}}
            continue
        m_id = re.search(r"\(id\s*=\s*([A-Z0-9-]+)\)", s)
        if m_id and s.startswith("**"):
            key = m_id.group(1)
            tws = None
            groups[key] = {"label": s.strip("* "), "blocks": {}}
            continue
        m_tws = re.match(r"TWS = (\d+)kts", s)
        if m_tws and key is not None:
            tws = int(m_tws.group(1))
            groups[key]["blocks"][tws] = []
            continue
        if key is not None and tws is not None and s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) == 8 and re.match(r"^[\d.]+°?$", cells[0]):
                groups[key]["blocks"][tws].append({
                    "twa": NUM(cells[0]), "btv": NUM(cells[1]), "vmg": NUM(cells[2]),
                    "aws": NUM(cells[3]), "awa": NUM(cells[4]), "heel": NUM(cells[5]),
                    "reef": NUM(cells[6]), "flat": NUM(cells[7]),
                })
    return groups


def _interp_btv(rows, twa):
    """Interpolate a sail's BTV at twa; None if outside the sail's TWA domain."""
    pts = sorted((r["twa"], r["btv"]) for r in rows)
    if not pts or twa < pts[0][0] or twa > pts[-1][0]:
        return None
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        if a0 <= twa <= a1:
            if a1 == a0:
                return b0
            return b0 + (b1 - b0) * (twa - a0) / (a1 - a0)
    return pts[-1][1]


def optimal_sail(sails, tws, twa):
    """Among sails whose domain covers (tws, twa), the one with the highest BTV."""
    best_id, best_btv = None, -1.0
    for sid, blocks in sails.items():
        rows = blocks.get(tws)
        if not rows:
            continue
        b = _interp_btv(rows, twa)
        if b is not None and b > best_btv:
            best_id, best_btv = sid, b
    return best_id


def sail_plan(sails, tws, best_rows):
    """Collapse the per-row optimal sail into TWA ranges for a quick sail plan."""
    segs = []
    for r in best_rows:
        sid = optimal_sail(sails, tws, r["twa"])
        if segs and segs[-1][0] == sid:
            segs[-1][2] = r["twa"]
        else:
            segs.append([sid, r["twa"], r["twa"]])
    parts = []
    for sid, lo, hi in segs:
        name = SAIL_NAMES.get(sid, sid or "—")
        parts.append(f"{name} TWA {lo:.0f}–{hi:.0f}°")
    return "  →  ".join(parts), {r["twa"]: optimal_sail(sails, tws, r["twa"]) for r in best_rows}


def write_guide(groups):
    best = groups["best"]["blocks"]
    sails = {sid: g["blocks"] for sid, g in groups.items() if sid != "best"}
    out = ["# SR33 \"C4\" — ORC Speed Guide (Best Performance polar + sail selection)\n"]
    out.append("Boat: SR33 *C4*, sail #CAN100 (ORC ref 03430004T3F). Source of truth for "
               "target boat speed AND sail selection. **BTV** = target boatspeed through "
               "water (kn); **VMG** = velocity made good; **AWS/AWA** = expected apparent "
               "wind at target; **Heel** = target heel (°); **Reef/Flat** = depowering "
               "(1.00 = full power, <1.00 = reef/flatten). **Sail** = the inventory sail that "
               "produces best performance at that TWS/TWA — use it to advise sail changes "
               "(when the optimal sail changes between angles, that's a crossover / peel).\n")
    out.append("## Sail inventory")
    for sid in ("J1-A", "A2-A", "A3-A", "S2-A"):
        if sid in groups:
            out.append(f"- **{sid}** — {groups[sid]['label']}")
    out.append("")
    for tws in sorted(best):
        rows = best[tws]
        if not rows:
            continue
        up = max((r for r in rows if r["twa"] <= 90), key=lambda r: r["vmg"], default=None)
        dn = max((r for r in rows if r["twa"] > 90), key=lambda r: r["vmg"], default=None)
        plan, row_sail = sail_plan(sails, tws, rows)
        out.append(f"\n## TWS {tws} kn")
        if up and dn:
            out.append(f"*Optimum beat: TWA {up['twa']:.1f}° → {up['btv']:.2f} kn "
                       f"(VMG {up['vmg']:.2f}). Optimum run: TWA {dn['twa']:.1f}° → "
                       f"{dn['btv']:.2f} kn (VMG {dn['vmg']:.2f}).*")
        out.append(f"*Sail plan: {plan}.*")
        out.append("\n| TWA | BTV | VMG | AWS | AWA | Heel | Reef | Flat | Sail |")
        out.append("|----:|----:|----:|----:|----:|-----:|-----:|-----:|:-----|")
        for r in rows:
            sid = row_sail.get(r["twa"]) or "—"
            out.append(f"| {r['twa']:.1f}° | {r['btv']:.2f} | {r['vmg']:.2f} | "
                       f"{r['aws']:.2f} | {r['awa']:.1f}° | {r['heel']:.1f}° | "
                       f"{r['reef']:.2f} | {r['flat']:.2f} | {sid} |")
    open(GUIDE, "w").write("\n".join(out) + "\n")
    return GUIDE


def _short(sid):
    """Sail id 'A3-A' -> crew shorthand 'A3'."""
    return (sid or "").split("-")[0] or sid


def crossover_zones(sails, tws, best_rows):
    """Per-TWS sail crossover bands: collapse the per-row optimal sail into contiguous TWA zones,
    with the crossover boundary at the midpoint between adjacent sail groups (the angle where you'd
    peel). The first band extends down to the optimum beat angle, the last up to the run angle."""
    groups = []                              # [(sail_id, twa_lo, twa_hi)] of consecutive same sail
    for r in best_rows:
        sid = optimal_sail(sails, tws, r["twa"])
        if groups and groups[-1][0] == sid:
            groups[-1][2] = r["twa"]
        else:
            groups.append([sid, r["twa"], r["twa"]])
    zones = []
    for i, (sid, lo, hi) in enumerate(groups):
        twa_min = lo if i == 0 else round((groups[i - 1][2] + lo) / 2.0, 1)
        twa_max = hi if i == len(groups) - 1 else round((hi + groups[i + 1][1]) / 2.0, 1)
        zones.append({"sail": sid, "short": _short(sid), "label": SAIL_NAMES.get(sid, sid or "—"),
                      "twa_min": twa_min, "twa_max": twa_max})
    return zones


def write_crossovers(groups):
    """Emit the per-TWS sail crossover table as JSON — the reviewable, onboard-loadable boat sail
    model the Lab optimizer attaches to each route leg and freezes into the playbook bundle."""
    best = groups["best"]["blocks"]
    sails = {sid: g["blocks"] for sid, g in groups.items() if sid != "best"}
    inv = [sid for sid in ("J1-A", "A2-A", "A3-A", "S2-A") if sid in groups]
    data = {
        "boat_id": "sr33",
        "source": "ORC certificate (data.orc.org SR33 'C4' CAN100, ref 03430004T3F)",
        "generated_by": "vps/agent/knowledge/build_speed_guide.py",
        "sail_names": {sid: SAIL_NAMES.get(sid, sid) for sid in inv},
        "inventory": [_short(sid) for sid in inv],
        "tws_buckets": sorted(best),
        "crossovers": {str(tws): crossover_zones(sails, tws, best[tws])
                       for tws in sorted(best) if best[tws]},
    }
    with open(CROSSOVERS, "w") as f:
        json.dump(data, f, indent=2)
    return CROSSOVERS


def write_sail_polars(groups):
    """Emit the PER-SAIL polar curves as JSON — the speed of EACH inventory sail across its TWA domain
    (not just the Best-Performance envelope). The Lab optimizer's sail-aware routing (routing fidelity
    2g) uses these to model the cost of HOLDING a sub-optimal sail through a crossover vs PEELING:
    the envelope is the max-over-sails speed (= the optimal sail's speed), but carrying, say, the A2
    past its crossover is SLOWER — that gap is the hold-vs-peel tradeoff, and it lives only here.
    Shape: {sails: {<short>: [[tws, twa, btv], ...]}} keyed by crew shorthand (J1/A2/A3/S2)."""
    sails = {sid: g["blocks"] for sid, g in groups.items() if sid != "best"}
    out = {}
    for sid in ("J1-A", "A2-A", "A3-A", "S2-A"):
        if sid not in sails:
            continue
        pts = [[tws, r["twa"], r["btv"]]
               for tws, rows in sorted(sails[sid].items()) for r in rows]
        out[_short(sid)] = pts
    data = {
        "boat_id": "sr33",
        "source": "ORC certificate (data.orc.org SR33 'C4' CAN100, ref 03430004T3F)",
        "generated_by": "vps/agent/knowledge/build_speed_guide.py",
        "sail_names": {_short(sid): SAIL_NAMES.get(sid, sid)
                       for sid in ("J1-A", "A2-A", "A3-A", "S2-A") if sid in sails},
        "sails": out,
    }
    with open(SAIL_POLARS, "w") as f:
        json.dump(data, f, indent=2)
    return SAIL_POLARS


def write_seed(groups):
    best = groups["best"]["blocks"]
    out = ["-- SR33 \"C4\" real ORC polar (Best Performance envelope). Generated by",
           "-- vps/agent/knowledge/build_speed_guide.py from the ORC certificate.",
           "-- Replaces synthetic placeholder polars. Idempotent.",
           "DELETE FROM polars WHERE boat_id = 'sr33';",
           "INSERT INTO polars (boat_id, tws, twa, target_stw, target_vmg) VALUES"]
    vals = [f"  ('sr33', {tws}, {r['twa']:.1f}, {r['btv']:.2f}, {r['vmg']:.2f})"
            for tws in sorted(best) for r in best[tws]]
    out.append(",\n".join(vals))
    out.append("ON CONFLICT (boat_id, tws, twa) DO UPDATE")
    out.append("  SET target_stw = EXCLUDED.target_stw, target_vmg = EXCLUDED.target_vmg;")
    open(SEED, "w").write("\n".join(out) + "\n")
    return SEED


if __name__ == "__main__":
    groups = parse_groups(open(SRC).read())
    print("parsed groups:", {k: f"{len(v['blocks'])} TWS" for k, v in groups.items()})
    print("wrote", write_guide(groups))
    print("wrote", write_seed(groups))
    print("wrote", write_crossovers(groups))
    print("wrote", write_sail_polars(groups))
