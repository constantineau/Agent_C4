"""Build the snapshot corpus → data/snapshots.jsonl.

A snapshot is a fixed strategy digest (the engine's picture/concordance/recommendation) plus a
human-readable situation for the labeling UI. Two sources:

  * SYNTHETIC (default, offline) — `synth.scenarios()` across the judgment space, hard-case-weighted.
    This is the pilot corpus; needs no boat, engine, LLM, or key.
  * ENGINE (opt-in, --from-engine) — pull live `/strategy` digests from a running onboard engine
    (bench or boat). This is how REAL snapshots enter once we log decision states; kept minimal here
    (one call per invocation) because the engine's digest is a point-in-time read.

Reproducible: synthetic snapshot_ids are content hashes, so re-running yields the identical corpus.

    python3 -m training.gen_snapshots                 # synthetic corpus
    python3 -m training.gen_snapshots --from-engine    # append one live engine digest
"""
import argparse

from . import config, schema, synth


def _situation_text(sc: dict, digest: dict) -> str:
    """Plain-language situation for the ranking UI — sets the scene the candidates respond to.
    NOT scored; it's context so a sailor can judge the assessment + recommendation."""
    cond = sc.get("cond") or {}
    pb = "playbook aboard" if sc.get("has_playbook", True) else "NO playbook aboard (practice/no gameplan)"
    hd = synth._wind_state(sc)["heading"]                    # boat's nominal compass heading
    board = (f" ({cond['tack']} {cond.get('board', 'tack')}, heading ~{hd}°)"
             if cond.get("tack") else "")
    lead = (f"On the {cond.get('leg','leg')}{board}, ~{cond.get('tws','?')} kts TWS, next mark "
            f"{cond.get('next_mark','?')} {cond.get('distance_nm','?')} nm — {pb}.")
    reads = [f"· {p['read']}" for p in digest.get("picture", []) if p.get("signal") != "concordance"]
    conc = digest.get("concordance", {})
    concline = f"Concordance: {conc.get('strength','none')} — {conc.get('note','')}".strip(" —")
    return "\n".join([lead] + reads + [concline])


def build_synthetic() -> list[dict]:
    snaps = []
    for sc in synth.scenarios(config.SYNTH_RANDOM_N, config.SYNTH_SEED):
        digest = synth.build_digest(sc)
        scenario = {k: v for k, v in sc.items() if k != "_i"}   # keep tag/cond/axes for the audit report
        situation = _situation_text(sc, digest)
        snap = schema.make_snapshot(digest, scenario, situation, source="synthetic")
        snap["scene"] = synth.build_scene(sc)                   # geometry frozen now (needs sc['_i'])
        snaps.append(snap)
    return snaps


def build_from_engine(route=None) -> list[dict]:
    """Pull one live `/strategy` digest from a running onboard engine (bench or boat)."""
    from copilot.engine_client import EngineClient  # lazy — only when actually hitting an engine
    engine = EngineClient()
    digest = engine.strategy(route)
    if not digest.get("available"):
        print(f"engine /strategy not available: {digest.get('error') or digest.get('assessment')}")
        return []
    digest.pop("_meta", None)
    scenario = {"tag": "engine_live", "route": route, "cond": {}}
    situation = _situation_text(scenario, digest)
    return [schema.make_snapshot(digest, scenario, situation, source="engine", route=route)]


def main():
    ap = argparse.ArgumentParser(description="Build the strategy-brief snapshot corpus.")
    ap.add_argument("--from-engine", action="store_true",
                    help="append one live digest from a running onboard engine (ENGINE_URL)")
    ap.add_argument("--route", default=None, help="route id for --from-engine")
    ap.add_argument("--append", action="store_true", help="append to the existing corpus (else overwrite)")
    args = ap.parse_args()

    existing = schema.read_jsonl(config.SNAPSHOTS) if args.append else []
    new = build_from_engine(args.route) if args.from_engine else build_synthetic()

    # De-dupe by snapshot_id (content hash) so re-runs / appends don't duplicate.
    by_id = {s["snapshot_id"]: s for s in existing}
    added = sum(1 for s in new if s["snapshot_id"] not in by_id)
    for s in new:
        by_id[s["snapshot_id"]] = s
    rows = list(by_id.values())

    schema.write_jsonl(config.SNAPSHOTS, rows)
    tags = {}
    for s in rows:
        t = "engine" if s["source"] == "engine" else "synthetic"
        tags[t] = tags.get(t, 0) + 1
    print(f"snapshots: {len(rows)} total (+{added} new) → {config.SNAPSHOTS}")
    print(f"  by source: {tags}")


if __name__ == "__main__":
    main()
