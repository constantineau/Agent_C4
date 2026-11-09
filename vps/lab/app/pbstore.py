"""Frozen-playbook store — persist the signed Lab-2b bundles on a writable volume.

A frozen bundle is the pre-race homework deployed onboard (posted to the Pi engine's
`/playbook/load`, which passes it to the copilot). A frozen bundle is IMMUTABLE BYTES (#249): its id
is `<race_id>__<course_id>__<digest>`, the digest being the sha256 of its signed content (the same
bytes its signature covers), and a file, once written, is never written again. Re-freezing makes a
new REVISION under a new id; Deploy pushes only the revision activated there (deploy.lock_in), so a
re-freeze changes nothing on the boat until someone activates it.

Old ids (`<race_id>__<start_epoch>`, before #249) still resolve: those files stay where they are,
byte for byte, and are listed as revisions like any other. Nothing writes to that name any more, so
an old lock keeps pointing at the bytes it had. The raw bytes are the signed artifact — `get_raw`
returns them verbatim so a download preserves the signature.
"""
import contextlib
import glob
import hashlib
import json
import os
import re
import tempfile

from . import atomicio

PLAYBOOK_DIR = os.environ.get("PLAYBOOK_DIR", "/srv/playbooks")
DIGEST_LEN = 12                          # hex chars of the content digest in the id


def _safe(s) -> str:
    return re.sub(r"[^a-z0-9_-]", "", str(s or "").lower())


def content_digest(bundle: dict) -> str:
    """sha256 over the bundle minus its `signature` — for a signed bundle this IS its signature
    value (synthesis.canonical_bytes), so the id names exactly the bytes the boat verifies."""
    content = {k: v for k, v in bundle.items() if k != "signature"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def bundle_id(bundle: dict) -> str:
    """`<race>__<course>__<digest>`: one id per distinct content, never reused for other bytes."""
    return (f"{_safe(bundle.get('race_id')) or 'race'}__{_safe(bundle.get('course_id')) or 'course'}"
            f"__{content_digest(bundle)[:DIGEST_LEN]}")


def legacy_id(bundle: dict) -> str:
    """The pre-#249 id (`<race>__<start_epoch>`), which re-freezing used to overwrite."""
    return f"{_safe(bundle.get('race_id')) or 'race'}__{int(bundle.get('start_epoch') or 0)}"


def is_legacy(pid: str) -> bool:
    return not re.fullmatch(r".+__.+__[0-9a-f]{%d}" % DIGEST_LEN, str(pid or ""))


def _path(pid: str) -> str:
    return os.path.join(PLAYBOOK_DIR, f"{_safe(pid)}.json")


def _fsync_dir(d):
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: str, data: bytes) -> bool:
    """Create `path` with `data` atomically, never replacing an existing file: the bytes go to a
    temp file, are fsynced, then hard-linked into place (fails if the name exists). A reader sees
    no file or the whole file. True if written, False if the file was already there."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        _fsync_dir(d)
        return True
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _sig_value(b):
    sig = b.get("signature")
    return sig.get("value") if isinstance(sig, dict) and sig.get("alg") == "sha256" else None


def save(bundle: dict) -> str:
    """Persist a (signed) bundle as a new revision; returns its id. The file IS the onboard-loadable
    artifact. Never overwrites: the same content frozen again keeps the first file's bytes (its
    signature stays the one any lock or push recorded)."""
    os.makedirs(PLAYBOOK_DIR, exist_ok=True)
    pid = bundle_id(bundle)
    data = json.dumps(bundle, indent=2).encode("utf-8")
    if not _write_new(_path(pid), data):
        old, digest = get(pid), content_digest(bundle)
        if not isinstance(old, dict) or content_digest(old) != digest \
                or (_sig_value(bundle) == digest and _sig_value(old) != digest):
            # the file under this id is damaged (it no longer holds the content its id names, or
            # its signature no longer matches it while this one does):
            # put the named content back, the one case a file is replaced. It keeps its place in
            # the revision order: the first freeze time (signed_at is outside the digest) and the
            # file's write time
            path, at = _path(pid), _mtime_ns(_path(pid))
            old_at = ((old or {}).get("signature") or {}) if isinstance(old, dict) else {}
            old_at = old_at.get("signed_at") if isinstance(old_at, dict) else None
            if isinstance(old_at, (int, float)) and isinstance(bundle.get("signature"), dict):
                bundle = dict(bundle, signature=dict(bundle["signature"], signed_at=old_at))
                data = json.dumps(bundle, indent=2).encode("utf-8")
            atomicio.write_bytes(path, data)
            if at:
                os.utime(path, ns=(at, at))
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


def _mtime_ns(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def list_bundles():
    """Summaries for the frozen-playbook list, newest first. `rev` numbers the revisions of each
    race + course in freeze order (1 = first)."""
    out = []
    for f in sorted(glob.glob(os.path.join(PLAYBOOK_DIR, "*.json"))):
        try:
            with open(f) as fh:
                b = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(b, dict):
            continue
        sig = b.get("signature")
        sig = sig if isinstance(sig, dict) else {}      # a damaged file lists as unsigned, no crash
        pid = os.path.splitext(os.path.basename(f))[0]
        out.append({
            "id": pid, "legacy": is_legacy(pid),
            "race_id": b.get("race_id"), "race_name": b.get("race_name"),
            "course_id": b.get("course_id"), "start_epoch": b.get("start_epoch"),
            "generated_at": b.get("generated_at"), "headline": b.get("headline"),
            "recommended": b.get("recommended"), "n_variants": len(b.get("variants", [])),
            "degraded": bool(b.get("degraded")), "warnings": list(b.get("warnings") or []),
            "signed": bool(sig.get("value")), "signed_at": sig.get("signed_at"),
            "signature": str(sig.get("value") or "")[:16],
            "synth_model": (b.get("provenance") or {}).get("synth_model"),
            "_mtime": _mtime_ns(f),
        })
    # freeze order: signed_at is whole seconds, so the file's own write time breaks ties
    order = lambda x: (x.get("signed_at") or 0, x["_mtime"], x["id"])  # noqa: E731
    seen = {}
    for x in sorted(out, key=order):
        k = (x.get("race_id"), x.get("course_id"))
        seen[k] = x["rev"] = seen.get(k, 0) + 1
        del x["_mtime"]
    out.sort(key=lambda x: x.get("generated_at") or 0, reverse=True)
    return out
