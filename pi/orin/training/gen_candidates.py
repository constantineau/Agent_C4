"""Per snapshot, generate N DIVERSE candidate briefs → data/candidates.jsonl.

A candidate differs from its snapshot's digest ONLY in the assessment + recommendation (the picture
and concordance are fixed engine facts). Four origins give sailors something real to rank:

  deterministic  the digest's own assessment + recommendation (the engine/no-LLM answer — the anchor)
  perturbed      a rule-flipped, plausibly-WORSE variant (guarantees a clear bottom to the ranking)
  base           the deployed 7B (qwen2.5) at RAISED temperature, N samples (the model we're improving)
  opus           the Opus reference (strong, not infallible)

deterministic + perturbed are offline & always present. base needs a reachable Ollama; opus needs a
key — both best-effort (skipped, never blocking). The seed/system-prompt are built to MATCH the live
copilot.strategy_brief path exactly, so what sailors rank is what the model actually produces in-race.

    python3 -m training.gen_candidates
"""
import json

from . import config, schema, teacher

# Reuse the EXACT copilot strategy prompt + schema + JSON extractor, so train == inference.
from copilot import copilot as cp
from copilot import playbook as playbook_mod
from copilot.llm import LLMClient, LLMUnavailable


def _seed(digest: dict) -> str:
    return ("STRATEGIC PICTURE (engine-computed facts — reuse these, invent nothing):\n"
            + json.dumps({"assessment": digest.get("assessment"),
                          "picture": digest.get("picture"),
                          "concordance": digest.get("concordance"),
                          "recommendation": digest.get("recommendation")}, separators=(",", ":")))


def _rec_fields(rec: dict) -> dict:
    """Keep just the recommendation fields the ranking + schema care about."""
    return {k: rec.get(k) for k in ("action", "vs_playbook", "rationale", "grounded_in",
                                    "urgency", "confidence", "target_variant") if rec.get(k) is not None}


# --- deterministic ---------------------------------------------------------------------------
def _deterministic(snap: dict) -> dict:
    d = snap["digest"]
    return schema.make_candidate(snap, "deterministic", d.get("assessment", ""),
                                 _rec_fields(d.get("recommendation") or {}),
                                 gen_meta={"source": "engine_digest"})


# --- rule-perturbed (a plausible WORSE call) -------------------------------------------------
_OPP = {"left": "right", "right": "left"}


def _perturbed(snap: dict) -> dict:
    d = snap["digest"]
    rec = d.get("recommendation") or {}
    action = (rec.get("action") or "").lower()
    vs = rec.get("vs_playbook")

    if action.startswith("hold") or vs == "no-plan" and "sail your phase" in action:
        # Miss the hold: over-eagerly switch/commit with false confidence.
        new = {"action": "Switch → the other side now", "vs_playbook": "on-plan",
               "rationale": "The breeze looks like it's going — commit and cross the fleet.",
               "grounded_in": ["get_selector"], "urgency": "now", "confidence": "high"}
        assessment = "Breeze is shifting our way — time to commit hard."
    elif action.startswith("switch") or action.startswith("off-book"):
        # Miss the switch/departure: sit on the plan and under-react.
        new = {"action": "Hold: the plan", "vs_playbook": "on-plan",
               "rationale": "Nothing decisive yet — stay conservative and hold station.",
               "grounded_in": ["get_selector"], "urgency": "monitor", "confidence": "low"}
        assessment = "Not much happening — hold what we've got."
    else:  # no-plan / work-the-side → flip the side
        side = next((s for s in ("left", "right") if s in action), "left")
        new = {"action": f"Work the {_OPP[side]} — that's where it's paying",
               "vs_playbook": rec.get("vs_playbook", "no-plan"),
               "rationale": "Gut call — the other side looks better from here.",
               "grounded_in": [], "urgency": "soon", "confidence": "med"}
        assessment = f"Feels like the {_OPP[side]} is the side."
    return schema.make_candidate(snap, "perturbed", assessment, new,
                                 gen_meta={"rule": "flip_call"})


# --- base model (the 7B we're improving) -----------------------------------------------------
def _base(snap: dict, llm: LLMClient, n: int) -> list[dict]:
    d = snap["digest"]
    pb = playbook_mod.load()
    system = cp._strategy_prompt(pb)
    seed = _seed(d)
    out = []
    for i in range(n):
        try:
            msg = llm.chat([{"role": "system", "content": system},
                            {"role": "user", "content": seed}],
                           schema=cp._STRATEGY_SCHEMA, temperature=config.BASE_TEMPERATURE)
            parsed = cp._extract_json(msg.get("content") or "")
        except LLMUnavailable as e:
            print(f"  [base] unreachable ({e}) — skipping base candidates")
            break
        if not parsed or "recommendation" not in parsed:
            continue
        out.append(schema.make_candidate(
            snap, "base", (parsed.get("assessment") or "").strip(),
            _rec_fields(parsed.get("recommendation") or {}),
            gen_meta={"model": llm.model, "temperature": config.BASE_TEMPERATURE, "sample": i},
            nonce=f"base{i}"))
    return out


# --- opus ------------------------------------------------------------------------------------
def _opus(snap: dict) -> dict | None:
    d = snap["digest"]
    pb = playbook_mod.load()
    obj = teacher.generate(cp._strategy_prompt(pb), _seed(d))
    if not obj:
        return None
    return schema.make_candidate(snap, "opus", (obj.get("assessment") or "").strip(),
                                 _rec_fields(obj.get("recommendation") or {}),
                                 gen_meta={"model": config.ANTHROPIC_MODEL}, nonce="opus")


def generate_for(snap: dict, llm: LLMClient | None) -> list[dict]:
    cands = []
    if "deterministic" in config.CAND_ORIGINS:
        cands.append(_deterministic(snap))
    if "perturbed" in config.CAND_ORIGINS:
        cands.append(_perturbed(snap))
    if "base" in config.CAND_ORIGINS and llm is not None:
        cands.extend(_base(snap, llm, config.BASE_SAMPLES))
    if "opus" in config.CAND_ORIGINS:
        c = _opus(snap)
        if c:
            cands.append(c)
    # De-dupe identical candidate_ids (e.g. base emitting the same text twice).
    seen, uniq = set(), []
    for c in cands:
        if c["candidate_id"] not in seen:
            seen.add(c["candidate_id"])
            uniq.append(c)
    return uniq


def main():
    snaps = schema.read_jsonl(config.SNAPSHOTS)
    if not snaps:
        print(f"no snapshots at {config.SNAPSHOTS} — run `python3 -m training.gen_snapshots` first")
        return

    llm = None
    if "base" in config.CAND_ORIGINS:
        llm = LLMClient()
        if not llm.reachable():
            print(f"  [base] LLM {llm.base_url} not reachable — base candidates will be skipped")
            llm = None

    all_cands, by_origin = [], {}
    for i, snap in enumerate(snaps):
        cands = generate_for(snap, llm)
        all_cands.extend(cands)
        for c in cands:
            by_origin[c["origin"]] = by_origin.get(c["origin"], 0) + 1
        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{len(snaps)} snapshots")

    schema.write_jsonl(config.CANDIDATES, all_cands)
    per = round(len(all_cands) / max(1, len(snaps)), 1)
    ungrounded = sum(1 for c in all_cands if not c["grounding"]["ok"])
    print(f"candidates: {len(all_cands)} ({per}/snapshot) → {config.CANDIDATES}")
    print(f"  by origin: {by_origin}")
    print(f"  ungrounded recommendations (flagged, kept for ranking): {ungrounded}")


if __name__ == "__main__":
    main()
