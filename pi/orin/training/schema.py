"""Canonical shapes + reproducible IDs + grounding helpers for the Phase-0 pipeline.

The unit a sailor ranks is a STRATEGY BRIEF whose *picture + concordance are FIXED* (the engine's
deterministic facts) and whose *assessment + recommendation* vary between candidates — exactly the
judgment call we want to teach (see docs/STRATEGY_LORA_PLAN.md §"What a LoRA can/cannot move").

Two records flow through the pipeline, both JSONL:

  SNAPSHOT  {snapshot_id, source, route, scenario, situation, digest}
    - source: "synthetic" | "engine"
    - scenario: tags describing the case (for coverage + the engine-audit report)
    - situation: human-readable facts for the labeling UI (NOT scored)
    - digest: the fixed strategy digest — assessment/picture/concordance/recommendation/caveats/...
              (the shape of vps/agent/app/strategy.get_strategy_signals / copilot.strategy_brief)

  CANDIDATE {candidate_id, snapshot_id, origin, assessment, recommendation, grounding, gen_meta}
    - origin: "deterministic" | "perturbed" | "base" | "opus"
    - grounding: {allowed[], cited[], ok}  (does the rec cite only real signal tools?)

IDs are content hashes so a snapshot/candidate produced today is byte-identical months later —
the reproducibility the reward-model flywheel (Plan §6) depends on.
"""
import hashlib
import json
import os

from . import config

SNAPSHOT_VERSION = 1
CANDIDATE_VERSION = 1


# --- reproducible IDs ------------------------------------------------------------------------
def _canon(obj) -> bytes:
    """Canonical bytes: sorted keys, no whitespace — same content → same hash, always."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def snapshot_id(digest: dict, scenario: dict, source: str) -> str:
    return "s_" + hashlib.sha1(_canon({"d": digest, "sc": scenario, "src": source})).hexdigest()[:16]


def candidate_id(snapshot_id_: str, origin: str, assessment: str, recommendation: dict,
                 nonce: str = "") -> str:
    payload = {"s": snapshot_id_, "o": origin, "a": assessment, "r": recommendation, "n": nonce}
    return "c_" + hashlib.sha1(_canon(payload)).hexdigest()[:16]


# --- grounding (mirrors copilot.strategy_brief's allow-list) ---------------------------------
def allowed_sources(digest: dict) -> set[str]:
    """The signal tools a recommendation on THIS digest may legitimately cite: the synthesis
    itself + whatever tools actually contributed a picture item."""
    allowed = {"get_strategy", "get_selector"}
    for item in digest.get("picture", []) or []:
        allowed.update(item.get("grounded_in") or [])
    return allowed & set(config.SIGNAL_TOOLS) | {"get_strategy", "get_selector"}


def grounding_status(recommendation: dict, digest: dict) -> dict:
    """Filter a candidate rec's grounded_in against the allow-list (the same discipline
    copilot.strategy_brief applies before accepting an LLM recommendation)."""
    allowed = allowed_sources(digest)
    cited = [g for g in (recommendation.get("grounded_in") or [])]
    kept = [g for g in cited if g in allowed]
    return {"allowed": sorted(allowed), "cited": cited, "kept": kept, "ok": bool(kept)}


# --- JSONL io --------------------------------------------------------------------------------
def write_jsonl(path: str, rows: list[dict]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def make_snapshot(digest: dict, scenario: dict, situation: str, source: str, route=None) -> dict:
    sid = snapshot_id(digest, scenario, source)
    return {"snapshot_id": sid, "version": SNAPSHOT_VERSION, "source": source, "route": route,
            "scenario": scenario, "situation": situation, "digest": digest}


def make_candidate(snap: dict, origin: str, assessment: str, recommendation: dict,
                   gen_meta: dict | None = None, nonce: str = "") -> dict:
    sid = snap["snapshot_id"]
    cid = candidate_id(sid, origin, assessment, recommendation, nonce)
    return {"candidate_id": cid, "version": CANDIDATE_VERSION, "snapshot_id": sid,
            "origin": origin, "assessment": assessment, "recommendation": recommendation,
            "grounding": grounding_status(recommendation, snap["digest"]),
            "gen_meta": gen_meta or {}}
