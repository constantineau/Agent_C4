"""Turn a snapshot + its candidates into a BLIND, human-readable payload for the ranker UI.

Two rules that protect label quality:
  * Human-readable — a sailor ranks the situation + the assessment/recommendation, never raw JSON.
  * BLIND — the candidate's `origin` (deterministic/base/opus/perturbed) is NEVER sent to the client,
    so nobody up-ranks "the Opus one" or dumps "the perturbed one" on sight. Candidate order is
    shuffled per (snapshot, labeler) to kill position bias — deterministically, so a reload is stable.
"""
import hashlib


def _game_plan(d: dict, has_pb: bool) -> dict:
    """The NOMINAL frozen game plan the candidate calls hold or depart from — the baseline the sailor
    judges against. Safe to show (it's the pre-race prior, not a candidate's in-race answer)."""
    if not has_pb:
        return {"has_playbook": False,
                "text": "No gameplan aboard (practice / no frozen plan) — reason from the live "
                        "conditions only; there's nothing pre-authored to hold or switch within."}
    rl = d.get("recommended_label")
    if rl:
        return {"has_playbook": True, "recommended": rl,
                "text": f"The frozen plan is the {rl} (recommended). Pre-authored side branches "
                        "(Left / Right) are aboard — hold it unless the picture justifies switching "
                        "to a branch, or an off-book move."}
    return {"has_playbook": True,
            "text": "A frozen gameplan with pre-authored side branches is aboard."}


def render_snapshot(snap: dict) -> dict:
    d = snap["digest"]
    has_pb = (snap.get("scenario") or {}).get("has_playbook", True)
    picture = [{"signal": p.get("signal"), "read": p.get("read"), "confidence": p.get("confidence")}
               for p in d.get("picture", []) or []]
    return {
        "snapshot_id": snap["snapshot_id"],
        "situation": snap.get("situation", ""),
        "game_plan": _game_plan(d, has_pb),
        "picture": picture,
        "concordance": d.get("concordance", {}),
        "caveats": d.get("caveats", []),
        "scenario_tag": (snap.get("scenario") or {}).get("tag"),
        "has_playbook": has_pb,
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
