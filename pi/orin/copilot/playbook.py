"""The frozen playbook — pre-race homework loaded onboard and never re-derived mid-race.

Lab-2 (the branching playbook bundle) is what *produces* this artifact: N pre-optimized
routing variants + a branching decision tree + per-variant rationale/tradeoffs, signed and
frozen at the gun. The copilot's job in-race is to SELECT and INTERPRET those pre-authored
variants — it must never originate strategy that isn't grounded in the playbook, the engine,
or common public data.

Lab-2b (`vps/lab/app/synthesis.py`) now EMITS that bundle — the `c4.playbook/v1` schema, signed
(sha256 over its canonical content). This module loads it if `PLAYBOOK_PATH` points at one, exposes
a compact text digest for the LLM system prompt, and otherwise reports "no playbook loaded" so the
copilot honestly restricts itself to interpreting live engine facts. `verify_signature()` recomputes
the SAME canonical bytes the Lab signed (bundle minus `signature`, sorted-key/no-space JSON), so a
bundle that arrives byte-for-byte verifies — surfaced (non-fatal) as `signed`/`signature_ok` so the
crew sees a tampered or unsigned playbook. Wiring a frozen bundle in is just dropping the file at
`PLAYBOOK_PATH`.
"""
import hashlib
import json
import os

from . import config


def _canonical_bytes(data: dict) -> bytes:
    """MUST match the Lab signer (`vps/lab/app/synthesis.canonical_bytes`): the bundle minus its
    `signature` field, JSON with sorted keys and no spaces. A bundle that arrives byte-for-byte from
    the Lab re-serializes identically here, so a valid signature verifies."""
    content = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(content, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def verify_signature(data: dict) -> bool:
    """True iff the bundle carries a sha256 signature matching its canonical content. Frozen-at-the-
    gun integrity — a tampered or truncated playbook fails this."""
    sig = (data or {}).get("signature") or {}
    if sig.get("alg") != "sha256" or not sig.get("value"):
        return False
    return hashlib.sha256(_canonical_bytes(data)).hexdigest() == sig["value"]


def _jib_band_text(j) -> str:
    """'J2 14–20 kn' from a {sail, tws_min?, tws_max?} band."""
    sail = j.get("sail", "?")
    lo, hi = j.get("tws_min"), j.get("tws_max")
    if lo is None and hi is not None:
        return f"{sail} <{hi}"
    if lo is not None and hi is None:
        return f"{sail} {lo}+"
    if lo is not None and hi is not None:
        return f"{sail} {lo}–{hi}"
    return sail


class Playbook:
    """A loaded (or empty) playbook bundle. `loaded` is False when no homework is aboard."""

    def __init__(self, data: dict | None):
        self.data = data or {}
        self.loaded = bool(data)
        # Signature is informational, not a gate: a valid bundle with no signature (a draft) still
        # loads, but `signature_ok` is surfaced in /health so the crew sees an unsigned/tampered one.
        self.signed = bool((self.data.get("signature") or {}).get("value"))
        self.signature_ok = verify_signature(self.data) if self.signed else None

    @property
    def race_id(self) -> str | None:
        return self.data.get("race_id")

    @property
    def variants(self) -> list[dict]:
        return self.data.get("variants", []) or []

    @property
    def boat_model(self) -> dict:
        """The reviewed boat sail/draft model the Lab froze in (polars source + per-TWS sail
        crossovers + draft) — the per-leg sail plans rest on this."""
        return self.data.get("boat_model", {}) or {}

    @staticmethod
    def _sail_plan_text(v) -> str:
        seq = v.get("sail_plan") or [{"sail": s} for s in
                                     [l.get("sail") for l in (v.get("route") or {}).get("legs", [])]
                                     if s]
        sails = [s.get("sail") for s in seq if s.get("sail")] if seq and isinstance(seq[0], dict) else []
        # de-dupe consecutive
        out = []
        for s in sails:
            if not out or out[-1] != s:
                out.append(s)
        return " → ".join(out)

    def digest(self, max_variants: int = 6) -> str:
        """A compact text summary for the LLM system prompt. Kept short — the 7B's context is
        the constraint, and the copilot SELECTS variants, it doesn't need every leg detail."""
        if not self.loaded:
            return (
                "NO PLAYBOOK LOADED. There is no pre-authored strategy aboard. Restrict yourself "
                "to interpreting the live engine facts (own instruments + common public forecast); "
                "do not originate a race strategy or routing plan of your own."
            )
        lines = [f"PLAYBOOK loaded for race '{self.race_id or 'unknown'}'. "
                 f"{len(self.variants)} pre-authored variant(s). You SELECT/INTERPRET these; "
                 "you do not invent new ones."]
        bm = self.boat_model
        if bm.get("sail_inventory"):
            jib = "; ".join(_jib_band_text(j) for j in bm.get("jib_crossovers", []))
            lines.append(f"Boat sail model: inventory {', '.join(bm['sail_inventory'])}, draft "
                         f"{bm.get('draft_ft', '?')} ft — sail calls come from this frozen crossover "
                         "table + the engine's live TWS/TWA, not invented."
                         + (f" Upwind jib by wind: {jib}." if jib else ""))
        for v in self.variants[:max_variants]:
            vid = v.get("id") or v.get("name") or "?"
            summary = v.get("summary") or v.get("rationale") or ""
            flips = v.get("what_flips_it") or v.get("triggers") or ""
            plan = self._sail_plan_text(v)
            line = f"- variant {vid}: {summary}"
            if plan:
                line += f" | sails: {plan}"
            if flips:
                line += f" | flips when: {flips}"
            lines.append(line)
        return "\n".join(lines)

    def variant_ids(self) -> list[str]:
        return [str(v.get("id") or v.get("name") or i) for i, v in enumerate(self.variants)]


def load(path: str | None = None) -> Playbook:
    path = (path if path is not None else config.PLAYBOOK_PATH) or ""
    if not path or not os.path.exists(path):
        return Playbook(None)
    try:
        with open(path) as f:
            return Playbook(json.load(f))
    except (OSError, json.JSONDecodeError):
        return Playbook(None)
