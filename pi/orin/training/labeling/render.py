"""Turn a snapshot + its candidates into a BLIND, human-readable payload for the ranker UI.

Two rules that protect label quality:
  * Human-readable — a sailor ranks the situation + the assessment/recommendation, never raw JSON.
  * BLIND — the candidate's `origin` (deterministic/base/opus/perturbed) is NEVER sent to the client,
    so nobody up-ranks "the Opus one" or dumps "the perturbed one" on sight. Candidate order is
    shuffled per (snapshot, labeler) to kill position bias — deterministically, so a reload is stable.
"""
import hashlib


def render_snapshot(snap: dict) -> dict:
    d = snap["digest"]
    picture = [{"signal": p.get("signal"), "read": p.get("read"), "confidence": p.get("confidence")}
               for p in d.get("picture", []) or []]
    return {
        "snapshot_id": snap["snapshot_id"],
        "situation": snap.get("situation", ""),
        "picture": picture,
        "concordance": d.get("concordance", {}),
        "caveats": d.get("caveats", []),
        "scenario_tag": (snap.get("scenario") or {}).get("tag"),
        "has_playbook": (snap.get("scenario") or {}).get("has_playbook", True),
    }


def render_candidate(cand: dict) -> dict:
    """Blind candidate card — NO origin, NO gen_meta."""
    rec = cand.get("recommendation") or {}
    return {
        "candidate_id": cand["candidate_id"],
        "assessment": cand.get("assessment", ""),
        "action": rec.get("action", ""),
        "rationale": rec.get("rationale", ""),
        "urgency": rec.get("urgency"),
        "confidence": rec.get("confidence"),
        "vs_playbook": rec.get("vs_playbook"),
        "grounded_in": rec.get("grounded_in") or [],
        "grounded_ok": cand.get("grounding", {}).get("ok", True),
    }


def shuffled_candidates(cands: list[dict], snapshot_id: str, labeler_id: str) -> list[dict]:
    """Deterministic per-(snapshot,labeler) shuffle: stable across reloads, varied across sailors."""
    def key(c):
        h = hashlib.sha1(f"{snapshot_id}:{labeler_id}:{c['candidate_id']}".encode()).hexdigest()
        return h
    return sorted(cands, key=key)
