"""Frozen-playbook store — persist the signed Lab-2b bundles on a writable volume.

A frozen bundle is the pre-race homework deployed onboard (it becomes the copilot's
`PLAYBOOK_PATH`). One file per freeze, id = `<race_id>__<start_epoch>` so re-freezing the same race
+ start overwrites (re-running the gameplan supersedes the old one). The raw bytes are the signed
artifact — `get_raw` returns them verbatim so a download/scp preserves the signature.
"""
import glob
import json
import os
import re

PLAYBOOK_DIR = os.environ.get("PLAYBOOK_DIR", "/srv/playbooks")


def _safe(s) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(s or "").lower())


def bundle_id(bundle: dict) -> str:
    return f"{_safe(bundle.get('race_id')) or 'race'}__{int(bundle.get('start_epoch') or 0)}"


def _path(pid: str) -> str:
    return os.path.join(PLAYBOOK_DIR, f"{_safe(pid)}.json")


def save(bundle: dict) -> str:
    """Persist a (signed) bundle; returns its id. The file IS the onboard-loadable artifact."""
    os.makedirs(PLAYBOOK_DIR, exist_ok=True)
    pid = bundle_id(bundle)
    with open(_path(pid), "w") as f:
        json.dump(bundle, f, indent=2)
    return pid


def get(pid: str):
    p = _path(pid)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def get_raw(pid: str):
    """The exact on-disk bytes (for download) — preserves whatever was signed."""
    p = _path(pid)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


def list_bundles():
    """Summaries for the frozen-playbook list, newest first."""
    out = []
    for f in sorted(glob.glob(os.path.join(PLAYBOOK_DIR, "*.json"))):
        try:
            with open(f) as fh:
                b = json.load(fh)
        except (OSError, ValueError):
            continue
        sig = b.get("signature") or {}
        out.append({
            "id": os.path.splitext(os.path.basename(f))[0],
            "race_id": b.get("race_id"), "race_name": b.get("race_name"),
            "course_id": b.get("course_id"), "start_epoch": b.get("start_epoch"),
            "generated_at": b.get("generated_at"), "headline": b.get("headline"),
            "recommended": b.get("recommended"), "n_variants": len(b.get("variants", [])),
            "signed": bool(sig.get("value")), "signed_at": sig.get("signed_at"),
            "synth_model": (b.get("provenance") or {}).get("synth_model"),
        })
    out.sort(key=lambda x: x.get("generated_at") or 0, reverse=True)
    return out
