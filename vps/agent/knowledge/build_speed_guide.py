#!/usr/bin/env python3
"""Distill one boat's ORC certificate export (data.orc.org Speed Guide, as markdown) into clean
artifacts for that boat (#306).

The export contains per-TWS tables: a "Best Performance" envelope plus one per sail in the RATED
wardrobe (e.g. Headsail J1-A, Symmetric S2-A, Asymmetric A2-A/A3-A). Each table has columns
TWA, BTV (target boatspeed), VMG, AWS, AWA, Heel, Reef, Flat.

The rated wardrobe need not be the boat's. An optional WARDROBE json (`--wardrobe`) says which
rated sails are aboard and what the crew calls them:
  {"title": "...", "boat_line": "...", "source": "...",
   "carried": [{"id": "J1-A", "short": "J1", "label": "Headsail/jib (J1)"}, ...]}
A rated sail not in `carried` is EXCLUDED (the envelope is rebuilt from carried sails so every
target is achievable). No wardrobe → every rated sail, named by its cert id ("A3-A" → "A3").

This script emits, for boat <id>:
  1. knowledge/<id>_speed_guide.md — the clean reference the coach and the sail dial read. Adds,
     per Best-Performance row, the OPTIMAL SAIL (derived by matching the per-sail polars) so
     the agent can advise sail selection and call crossovers / sail changes.
  2. db/seed/polars_<id>.sql — real polars (target_stw=BTV, target_vmg=VMG).
  3. db/seed/<id>_crossovers.json, db/seed/<id>_sail_polars.json — the sail model.
Name them in the boat profile's `polar_files`.

Run:  python3 vps/agent/knowledge/build_speed_guide.py <cert.md> <boat_id> [--wardrobe W.json]
e.g.  python3 vps/agent/knowledge/build_speed_guide.py vps/agent/knowledge/C4_boatspeed_gospel.md \
          sr33 --wardrobe vps/agent/knowledge/sr33_wardrobe.json
"""
import argparse
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SEED_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "db", "seed"))

NUM = lambda s: float(s.replace("°", "").strip())

# Set per boat by configure() (from the command line): the boat, its wardrobe, the outputs.
BOAT_ID = TITLE = BOAT_LINE = SOURCE = None
GUIDE = SEED = CROSSOVERS = SAIL_POLARS = None
EXCLUDED = set()          # rated sails not aboard
CARRIED = ()              # rated sails aboard (cert ids), in the guide's order
SAIL_NAMES = {}           # sail id -> crew-friendly label
SHORTS = {}               # sail id -> crew shorthand (the crew's name, not the cert's)


def configure(boat_id, wardrobe=None, rated=(), guide_dir=HERE, seed_dir=SEED_DIR):
    """Point the writers at boat `boat_id` and its wardrobe (see the module doc). `rated` = the
    cert's sail ids, used when there is no wardrobe (every rated sail aboard)."""
    global BOAT_ID, TITLE, BOAT_LINE, SOURCE, GUIDE, SEED, CROSSOVERS, SAIL_POLARS
    global EXCLUDED, CARRIED, SAIL_NAMES, SHORTS
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", boat_id or ""):
        raise SystemExit(f"bad boat id: {boat_id!r}")
    w = wardrobe or {}
    carried = w.get("carried") or [{"id": sid} for sid in rated]
    BOAT_ID = boat_id
    TITLE = w.get("title") or boat_id
    BOAT_LINE = w.get("boat_line") or f"Boat: {TITLE}."
    SOURCE = w.get("source") or "ORC certificate"
    CARRIED = tuple(c["id"] for c in carried)
    EXCLUDED = {sid for sid in rated if sid not in CARRIED}
    SAIL_NAMES = {c["id"]: c["label"] for c in carried if c.get("label")}
    SHORTS = {c["id"]: c["short"] for c in carried if c.get("short")}
    GUIDE = os.path.join(guide_dir, f"{boat_id}_speed_guide.md")
    SEED = os.path.join(seed_dir, f"polars_{boat_id}.sql")
    CROSSOVERS = os.path.join(seed_dir, f"{boat_id}_crossovers.json")
    SAIL_POLARS = os.path.join(seed_dir, f"{boat_id}_sail_polars.json")


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


def _interp_row(rows, twa):
    """Interpolate a full table row (all numeric columns) at twa; None if outside the domain."""
    pts = sorted(rows, key=lambda r: r["twa"])
    if not pts or twa < pts[0]["twa"] or twa > pts[-1]["twa"]:
        return None
    for r0, r1 in zip(pts, pts[1:]):
        if r0["twa"] <= twa <= r1["twa"]:
            span = r1["twa"] - r0["twa"]
            f = 0.0 if span == 0 else (twa - r0["twa"]) / span
            return {k: r0[k] + (r1[k] - r0[k]) * f for k in r0}
    return dict(pts[-1])


def rebuild_best(groups):
    """Rebuild the Best-Performance envelope from the CARRIED sails only. The cert's own
    envelope is max-over-the-whole-rated-wardrobe — with the A2 excluded, its reaching rows
    promise speeds the boat can't sail, which would poison targets, polar_pct and routing.
    Keeps the cert's TWA sampling (incl. optimum beat/run angles); each row takes every
    column from whichever carried sail is fastest there."""
    best = groups["best"]["blocks"]
    sails = {sid: g["blocks"] for sid, g in groups.items()
             if sid != "best" and sid not in EXCLUDED}
    rebuilt = {}
    for tws, rows in best.items():
        out = []
        for r in rows:
            cand = []
            for sid, blocks in sails.items():
                ir = _interp_row(blocks.get(tws) or [], r["twa"])
                if ir is not None:
                    cand.append(ir)
            out.append(max(cand, key=lambda c: c["btv"]) if cand else dict(r))
        rebuilt[tws] = out
    groups["best"]["blocks"] = rebuilt
    return groups


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
    out = [f"# {TITLE} — ORC Speed Guide (Best Performance polar + sail selection)\n"]
    out.append(f"{BOAT_LINE} Source of truth for "
               "target boat speed AND sail selection. **BTV** = target boatspeed through "
               "water (kn); **VMG** = velocity made good; **AWS/AWA** = expected apparent "
               "wind at target; **Heel** = target heel (°); **Reef/Flat** = depowering "
               "(1.00 = full power, <1.00 = reef/flatten). **Sail** = the inventory sail that "
               "produces best performance at that TWS/TWA — use it to advise sail changes "
               "(when the optimal sail changes between angles, that's a crossover / peel).\n")
    out.append("## Sail inventory")
    for sid in CARRIED:
        if sid in groups:
            out.append(f"- **{_short(sid)}** (cert id {sid}) — {groups[sid]['label']}")
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
            sid = row_sail.get(r["twa"])
            out.append(f"| {r['twa']:.1f}° | {r['btv']:.2f} | {r['vmg']:.2f} | "
                       f"{r['aws']:.2f} | {r['awa']:.1f}° | {r['heel']:.1f}° | "
                       f"{r['reef']:.2f} | {r['flat']:.2f} | {_short(sid) if sid else '—'} |")
    open(GUIDE, "w").write("\n".join(out) + "\n")
    return GUIDE


def _short(sid):
    """Sail id -> crew shorthand ('A3-A' -> 'A3', 'S2-A' -> 'S1' — the crew's names)."""
    return SHORTS.get(sid) or (sid or "").split("-")[0] or sid


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


# Two sails are a "toss-up" at an angle when both are within this fraction of the best sail's BTV.
# The crossover zones are winner-take-all, so a sail that's optimal by hundredths of a knot at every
# sampled angle erases the runner-up entirely (the cert splits adjacent kites by ~0.03 kn near a
# crossover). Overlap bands surface those near-ties so the chart can show "carry either".
OVERLAP_TOL = float(os.environ.get("XOVER_OVERLAP_TOL", "0.015"))


def cooptimal(sails, tws, twa, tol=OVERLAP_TOL):
    """Sail ids whose BTV at (tws, twa) is within `tol` of the best sail's BTV (the winner included)."""
    scored = []
    for sid, blocks in sails.items():
        b = _interp_btv(blocks.get(tws) or [], twa)
        if b is not None:
            scored.append((sid, b))
    if not scored:
        return []
    best = max(b for _, b in scored)
    return [sid for sid, b in scored if best - b <= tol * best]


def overlap_bands(sails, tws, best_rows, inv, tol=OVERLAP_TOL):
    """Per-TWS 'toss-up' bands: contiguous TWA ranges where two ADJACENT-inventory sails are BOTH
    co-optimal (within `tol` of the best). Surfaces sails the winner-take-all zones hide on a near-tie.
    Boundaries extend to the neighbour-row midpoints (like the zones) so a single-row tie is still
    visible. Sail ids are crew shorthand; the upwind jib stays 'J1' (specialised per TWS downstream)."""
    order = list(inv)                                 # cert ids in inventory order (J1-A, A3-A, …)
    twas = [r["twa"] for r in best_rows]
    n = len(best_rows)
    bands = []
    for s1, s2 in zip(order, order[1:]):              # adjacent pairs only (J1/A3, A3/S1)
        marked = []
        for r in best_rows:
            co = cooptimal(sails, tws, r["twa"], tol)
            marked.append(s1 in co and s2 in co)
        i = 0
        while i < n:
            if not marked[i]:
                i += 1
                continue
            j = i
            while j + 1 < n and marked[j + 1]:
                j += 1
            twa_min = twas[i] if i == 0 else round((twas[i - 1] + twas[i]) / 2.0, 1)
            twa_max = twas[j] if j == n - 1 else round((twas[j] + twas[j + 1]) / 2.0, 1)
            bands.append({"sails": [_short(s1), _short(s2)], "twa_min": twa_min, "twa_max": twa_max})
            i = j + 1
    bands.sort(key=lambda b: b["twa_min"])
    return bands


def write_crossovers(groups):
    """Emit the per-TWS sail crossover table as JSON — the reviewable, onboard-loadable boat sail
    model the Lab optimizer attaches to each route leg and freezes into the playbook bundle."""
    best = groups["best"]["blocks"]
    sails = {sid: g["blocks"] for sid, g in groups.items() if sid != "best"}
    inv = [sid for sid in CARRIED if sid in groups]
    data = {
        "boat_id": BOAT_ID,
        "source": SOURCE,
        "generated_by": "vps/agent/knowledge/build_speed_guide.py",
        "sail_names": {sid: SAIL_NAMES.get(sid, sid) for sid in inv},
        "inventory": [_short(sid) for sid in inv],
        "tws_buckets": sorted(best),
        "crossovers": {str(tws): crossover_zones(sails, tws, best[tws])
                       for tws in sorted(best) if best[tws]},
        "overlaps": {str(tws): overlap_bands(sails, tws, best[tws], inv)
                     for tws in sorted(best) if best[tws]},
    }
    with open(CROSSOVERS, "w") as f:
        json.dump(data, f, indent=2)
    return CROSSOVERS


def write_sail_polars(groups):
    """Emit the PER-SAIL polar curves as JSON — the speed of EACH inventory sail across its TWA domain
    (not just the Best-Performance envelope). The Lab optimizer's sail-aware routing (routing fidelity
    2g) uses these to model the cost of HOLDING a sub-optimal sail through a crossover vs PEELING:
    the envelope is the max-over-sails speed (= the optimal sail's speed), but carrying, say, the A3
    past its crossover is SLOWER — that gap is the hold-vs-peel tradeoff, and it lives only here.
    Shape: {sails: {<short>: [[tws, twa, btv], ...]}} keyed by crew shorthand (J1/A3/S1)."""
    sails = {sid: g["blocks"] for sid, g in groups.items() if sid != "best"}
    out = {}
    for sid in CARRIED:
        if sid not in sails:
            continue
        pts = [[tws, r["twa"], r["btv"]]
               for tws, rows in sorted(sails[sid].items()) for r in rows]
        out[_short(sid)] = pts
    data = {
        "boat_id": BOAT_ID,
        "source": SOURCE,
        "generated_by": "vps/agent/knowledge/build_speed_guide.py",
        "sail_names": {_short(sid): SAIL_NAMES.get(sid, sid)
                       for sid in CARRIED if sid in sails},
        "sails": out,
    }
    with open(SAIL_POLARS, "w") as f:
        json.dump(data, f, indent=2)
    return SAIL_POLARS


def write_seed(groups):
    best = groups["best"]["blocks"]
    out = [f"-- {TITLE} real ORC polar (Best Performance envelope). Generated by",
           "-- vps/agent/knowledge/build_speed_guide.py from the ORC certificate.",
           "-- Replaces synthetic placeholder polars. Idempotent.",
           f"DELETE FROM polars WHERE boat_id = '{BOAT_ID}';",
           "INSERT INTO polars (boat_id, tws, twa, target_stw, target_vmg) VALUES"]
    vals = [f"  ('{BOAT_ID}', {tws}, {r['twa']:.1f}, {r['btv']:.2f}, {r['vmg']:.2f})"
            for tws in sorted(best) for r in best[tws]]
    out.append(",\n".join(vals))
    out.append("ON CONFLICT (boat_id, tws, twa) DO UPDATE")
    out.append("  SET target_stw = EXCLUDED.target_stw, target_vmg = EXCLUDED.target_vmg;")
    open(SEED, "w").write("\n".join(out) + "\n")
    return SEED


def main(argv=None):
    ap = argparse.ArgumentParser(description="One boat's ORC speed guide + sail model.")
    ap.add_argument("cert", help="the ORC Speed Guide export, as markdown")
    ap.add_argument("boat_id", help="the boat's id (names the output files)")
    ap.add_argument("--wardrobe", help="json: the sails aboard and the crew's names for them")
    ap.add_argument("--guide-dir", default=HERE)
    ap.add_argument("--seed-dir", default=SEED_DIR)
    a = ap.parse_args(argv)
    with open(a.cert, encoding="utf-8") as f:
        groups = parse_groups(f.read())
    wardrobe = None
    if a.wardrobe:
        with open(a.wardrobe, encoding="utf-8") as f:
            wardrobe = json.load(f)
    configure(a.boat_id, wardrobe, rated=[k for k in groups if k != "best"],
              guide_dir=a.guide_dir, seed_dir=a.seed_dir)
    print("parsed groups:", {k: f"{len(v['blocks'])} TWS" for k, v in groups.items()})
    dropped = [k for k in groups if k in EXCLUDED]
    groups = {k: v for k, v in groups.items() if k not in EXCLUDED}
    groups = rebuild_best(groups)
    print("excluded (rated but not aboard):", dropped or "none")
    print("wrote", write_guide(groups))
    print("wrote", write_seed(groups))
    print("wrote", write_crossovers(groups))
    print("wrote", write_sail_polars(groups))


if __name__ == "__main__":
    main()
