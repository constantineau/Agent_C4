"""Turn labeler NOTES into human-reviewable PROPOSED improvements — LLM-drafted, never auto-applied.

Free-text notes are the richest qualitative signal from the ranker, but they were write-only: nothing
read them (make_pairs uses only the ranking order + calibration). This closes that gap. It reads every
note with its snapshot context, asks the teacher LLM (Opus, best-effort) to CLUSTER the notes into
themes and DRAFT concrete proposals — what to change in the situation content / the ranking rubric /
the engine, why, and which notes support it — and stores them as `open` proposals for a HUMAN to accept
or dismiss.

PROPOSE-ONLY, like the Lab-4 learning loop: it mutates NOTHING (not snapshots, not the rubric, not the
engine). A person implements the accepted proposals. If no LLM/key is reachable, a deterministic
fallback still emits one proposal per note (ungrouped) so a review digest always exists.

    python3 -m training.notes_review                 # draft proposals from the notes, print the digest
    python3 -m training.notes_review --list          # show stored proposals + status
    python3 -m training.notes_review --accept <id>   # a human accepts a proposal
    python3 -m training.notes_review --dismiss <id>
"""
import hashlib
import json
import sys
import time

from . import config, schema
from .labeling import store


# ---------------------------------------------------------------------------- gather
def collect_notes() -> list[dict]:
    """Every ranking that carries a note, joined to its snapshot for context. Orphaned notes (the
    snapshot was regenerated away) are KEPT — the feedback is still valuable — but flagged stale."""
    snaps = {s["snapshot_id"]: s for s in schema.read_jsonl(config.SNAPSHOTS)}
    out = []
    for r in store.all_rankings():
        note = (r.get("notes") or "").strip()
        if not note:
            continue
        snap = snaps.get(r["snapshot_id"])
        out.append({
            "note": note,
            "labeler_id": r["labeler_id"],
            "snapshot_id": r["snapshot_id"],
            "scenario_tag": (snap or {}).get("scenario", {}).get("tag"),
            "situation": (snap or {}).get("situation", ""),
            "stale": snap is None,           # note is on a snapshot no longer in the corpus
        })
    return out


# ---------------------------------------------------------------------------- LLM draft
_SYSTEM = (
    "You are a data-quality lead for a sailing-tactics AI training set. Expert sailors rank candidate "
    "tactical briefs for synthetic race SITUATIONS and sometimes leave a NOTE. Your job: read the notes "
    "(each with the situation it was left on) and turn them into a small set of concrete, actionable "
    "PROPOSALS to improve the training data or system — never vague summaries. Cluster related notes "
    "into one proposal. For each proposal give: `theme` (short), `target` (one of: situation_content = "
    "what the situation text shows the sailor; rubric = the ranking instructions/criteria; engine = a "
    "missing/computed signal the deterministic engine should produce; other), `proposed_change` (a "
    "specific, implementable edit), `rationale` (why, tied to the notes), and `supporting_notes` (the "
    "list of note indices it came from). Only propose what the notes actually justify. "
    "Return STRICT JSON only: {\"proposals\":[{\"theme\":str,\"target\":str,\"proposed_change\":str,"
    "\"rationale\":str,\"supporting_notes\":[int]}]}")


def _notes_block(notes: list[dict]) -> str:
    lines = []
    for i, n in enumerate(notes):
        ctx = n["situation"] if not n["stale"] else "(snapshot regenerated — situation unavailable)"
        lines.append(f"[{i}] note: {n['note']}\n     situation: {ctx}")
    return "\n".join(lines)


def _llm_propose(notes: list[dict]) -> list[dict] | None:
    """Ask Opus to cluster the notes into proposals. None on any trouble (missing key/SDK/parse)."""
    if not config.ANTHROPIC_API_KEY:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL, max_tokens=2048, system=_SYSTEM,
            messages=[{"role": "user", "content": "Notes:\n" + _notes_block(notes)}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    except Exception as e:  # noqa: BLE001 — best-effort; never crash
        print(f"  [opus] notes-review skipped: {e}")
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    props = obj.get("proposals") if isinstance(obj, dict) else None
    return props if isinstance(props, list) else None


def _fallback_propose(notes: list[dict]) -> list[dict]:
    """No LLM: surface each note as its own raw proposal so a human still gets a review digest."""
    return [{"theme": "raw note (no LLM clustering)", "target": "other",
             "proposed_change": n["note"], "rationale": "verbatim labeler note — review by hand",
             "supporting_notes": [i]} for i, n in enumerate(notes)]


# ---------------------------------------------------------------------------- store (append-only)
def load_proposals() -> list[dict]:
    return schema.read_jsonl(config.NOTE_PROPOSALS)


def _pid(p: dict) -> str:
    return "np_" + hashlib.sha1(
        (str(p.get("theme", "")) + "|" + str(p.get("proposed_change", ""))).encode()).hexdigest()[:10]


def _save(proposals: list[dict]) -> None:
    schema.write_jsonl(config.NOTE_PROPOSALS, proposals)


def set_status(pid: str, status: str) -> bool:
    props = load_proposals()
    hit = False
    for p in props:
        if p["id"] == pid:
            p["status"] = status
            p["reviewed_at"] = time.time()
            hit = True
    if hit:
        _save(props)
    return hit


# ---------------------------------------------------------------------------- orchestrate
def propose(now: float | None = None) -> dict:
    """Draft proposals from the current notes, MERGE into the store (dedup by id, keep prior status),
    and return {n_notes, drafted, new, proposals}. Never mutates snapshots/rubric/engine."""
    now = now if now is not None else time.time()
    notes = collect_notes()
    if not notes:
        return {"n_notes": 0, "drafted": 0, "new": 0, "proposals": [],
                "note": "no labeler notes yet — nothing to propose"}

    drafted = _llm_propose(notes)
    used_llm = drafted is not None
    if not used_llm:
        drafted = _fallback_propose(notes)

    existing = {p["id"]: p for p in load_proposals()}
    new = 0
    for d in drafted:
        # resolve the supporting-note indices to their (snapshot, labeler, text) for traceability
        idxs = [i for i in (d.get("supporting_notes") or []) if isinstance(i, int) and 0 <= i < len(notes)]
        d_full = {
            "id": _pid(d), "created_at": now, "status": "open", "source": "opus" if used_llm else "raw",
            "theme": d.get("theme", ""), "target": d.get("target", "other"),
            "proposed_change": d.get("proposed_change", ""), "rationale": d.get("rationale", ""),
            "supporting_notes": [{"snapshot_id": notes[i]["snapshot_id"],
                                  "labeler_id": notes[i]["labeler_id"], "note": notes[i]["note"]}
                                 for i in idxs],
        }
        if d_full["id"] not in existing:      # keep a human's prior accept/dismiss on re-runs
            existing[d_full["id"]] = d_full
            new += 1
    props = sorted(existing.values(), key=lambda p: p.get("created_at", 0))
    _save(props)
    return {"n_notes": len(notes), "drafted": len(drafted), "new": new,
            "used_llm": used_llm, "proposals": props}


def _print_digest(res: dict) -> None:
    if res.get("n_notes", 0) == 0:
        print(res.get("note")); return
    src = "Opus-clustered" if res.get("used_llm") else "raw (no LLM — deterministic fallback)"
    print(f"notes reviewed: {res['n_notes']}  ·  proposals: {len(res['proposals'])} "
          f"({res['new']} new this run)  ·  {src}")
    print(f"stored → {config.NOTE_PROPOSALS}\n")
    for p in res["proposals"]:
        mark = {"open": "· ", "accepted": "✓ ", "dismissed": "✗ "}.get(p.get("status"), "· ")
        print(f"{mark}[{p['id']}] ({p['status']}) <{p['target']}> {p['theme']}")
        print(f"    change: {p['proposed_change']}")
        print(f"    why:    {p['rationale']}")
        for s in p.get("supporting_notes", []):
            print(f"    ← {s['labeler_id']}: \"{s['note']}\"")
        print()
    print("A human accepts/dismisses:  python3 -m training.notes_review --accept|--dismiss <id>")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--list":
        _print_digest({"n_notes": 1, "proposals": load_proposals(), "new": 0, "used_llm": True})
        return
    if args and args[0] in ("--accept", "--dismiss") and len(args) >= 2:
        status = "accepted" if args[0] == "--accept" else "dismissed"
        ok = set_status(args[1], status)
        print(f"{args[1]} → {status}" if ok else f"no proposal with id {args[1]}")
        return
    _print_digest(propose())


if __name__ == "__main__":
    main()
