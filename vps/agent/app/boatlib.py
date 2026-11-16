"""The boat library aboard, and which boat is being sailed (#320).

LIBRARY. Every boat profile the Lab holds, each with its own polar block (shared/boat_polar.py:
the same shape the Deploy push carries). The engine pulls it from the Lab by itself whenever the
Pi has internet: `GET <LAB_URL>/api/boats/library`, with the Pi's ingest token as the bearer (the
credential the uplink and backfill already use). The Pi pulls because it may sit behind NAT. The
library lives in one file on the engine volume, so it survives restarts and works offline. It is
never fetched or replaced while racing (RRS 41: nothing new from shore mid-race).

CURRENT BOAT. The boat the engine's % of polar, targets, sail dial, the coach (through those) and
the Orin re-route are for:
  1. the open LOG session's boat (the crew picks it on the iPad; "Other boat" is a typed name);
  2. no session open: the last boat used, the newer of the last session's boat and the last
     Deploy push's boat (a push is the Lab naming the boat that races next);
  3. neither: this kit's own BOAT_ID.
Its polar: the Deploy push's block when the push is for that boat and newer than the library's
copy, else the library's. Never another boat's. None (an "Other boat", or a boat the library
doesn't have) → "No polar for this boat."

Recorded rows stay tagged by the kit (BOAT_ID). Only the session names the boat sailed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request

from shared import boat_polar as BP
from shared import boat_profile

SCHEMA = "c4.boat-library/v1"
NO_POLAR = BP.NO_POLAR
OTHER = "other"
MAX_BYTES = 4 * 1024 * 1024          # the whole library file; sr33 + its polar is ~25 KB
MAX_BOATS = 100
NAME_MAX = 40
SYNC_S = float(os.environ.get("BOATLIB_SYNC_S", "900"))        # between good pulls
RETRY_S = float(os.environ.get("BOATLIB_RETRY_S", "120"))      # after a failed pull
TIMEOUT_S = float(os.environ.get("BOATLIB_TIMEOUT_S", "20"))
SESSION_TTL_S = 30.0

NOT_SET = "Boat library: LAB_URL not set."
RACING = "Racing: boat library not updated."

_LOCK = threading.Lock()
_GEN = [0]                       # bumped on a session change or a new library
_STATUS = {"at": None, "ok": None, "note": None}
_SESSION = {"at": 0.0, "gen": -1, "val": None}
_LIB_MEMO = {"memo": None}       # (file key, doc)


def _path():
    base = os.path.dirname(os.environ.get("ENGINE_DB", "/var/lib/c4/engine/engine.db"))
    return os.path.join(base, "boat-library.json")


def kit_id():
    """This install's own boat id (the BOAT_ID every recorded row is tagged with)."""
    return (os.environ.get("BOAT_ID") or "").strip() or None


def generation():
    """Changes whenever the current boat or the library changes (polar caches key on it)."""
    return _GEN[0]


def bump():
    """A session started or ended, or the library changed: re-read the current boat at once."""
    _GEN[0] += 1


def clean_name(raw):
    """A crew-typed boat name: printable, trimmed, at most NAME_MAX characters, or None."""
    s = "".join(ch for ch in str(raw or "") if ch.isprintable()).strip()[:NAME_MAX].strip()
    return s or None


# ---- the library file ---------------------------------------------------------------------------

def entry_errors(e):
    """Why one library entry can't be used, or [] (a bad entry is dropped, never half-used)."""
    if not isinstance(e, dict):
        return ["bad entry"]
    p = e.get("profile")
    if not isinstance(p, dict) or not isinstance(p.get("boat_id"), str) or not p["boat_id"]:
        return ["no profile"]
    try:
        bad = boat_profile.shape_errors(p) or boat_profile.validate(p)[0]
    except (ValueError, TypeError, OverflowError):
        bad = ["bad values"]
    if bad:
        return [f"profile: {bad[0]}"]
    if e.get("boat_version") != boat_profile.version(p):
        return ["profile version doesn't match"]
    pol = e.get("polar")
    if pol is not None:
        errs = BP.check(pol, p["boat_id"])
        if errs:
            return errs
    return []


def _write(text):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".boat-library-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read():
    """The stored library {schema, synced_at, boats: {id: entry}}, or None. Memoised on the file."""
    try:
        st = os.stat(_path())
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    memo = _LIB_MEMO["memo"]
    if memo is not None and memo[0] == key:
        return memo[1]
    try:
        with open(_path()) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict) or not isinstance(doc.get("boats"), dict):
        doc = None
    _LIB_MEMO["memo"] = (key, doc)
    return doc


def entries():
    """{boat_id: entry} of the library aboard ({} when none has synced yet)."""
    doc = _read()
    return dict(doc["boats"]) if doc else {}


def store(doc, now=None):
    """Keep a library the Lab served → {stored, boats, dropped, note}. Every entry is checked on
    its own: a bad one is dropped and named, the rest are kept. `got_at` (when this Pi first held
    an entry's polar version) is carried over while the version is unchanged: it is how the engine
    tells whether a Deploy push or the library copy is newer. Refused while racing."""
    now = time.time() if now is None else float(now)
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA or not isinstance(
            doc.get("boats"), list):
        return {"stored": False, "note": "not a c4.boat-library/v1 library"}
    old = entries()
    boats, dropped = {}, []
    for e in doc["boats"][:MAX_BOATS]:
        errs = entry_errors(e)
        bid = (e.get("profile") or {}).get("boat_id") if isinstance(e, dict) else None
        if errs:
            dropped.append({"boat_id": str(bid)[:40] if bid else None, "why": errs[0]})
            continue
        ver = (e.get("polar") or {}).get("version")
        prev = old.get(bid) or {}
        same = prev.get("polar_version") == ver and prev.get("boat_version") == e["boat_version"]
        boats[bid] = {"profile": e["profile"], "boat_version": e["boat_version"],
                      "polar": e.get("polar"), "polar_version": ver,
                      "got_at": prev.get("got_at") if same and prev.get("got_at") else now}
    text = json.dumps({"schema": SCHEMA, "synced_at": now, "built_at": doc.get("built_at"),
                       "boats": boats}, separators=(",", ":"), allow_nan=False)
    if len(text) > MAX_BYTES:
        return {"stored": False, "note": "Boat library too large."}
    from . import racegate
    with _LOCK, racegate.hold() as racing:
        if racing:
            return {"stored": False, "note": RACING}
        _write(text)
        bump()
    return {"stored": True, "boats": sorted(boats), "dropped": dropped}


# ---- pulling it from the Lab --------------------------------------------------------------------

def lab_url():
    return (os.environ.get("LAB_URL") or "").strip().rstrip("/") or None


def _token():
    return (os.environ.get("BOATLIB_TOKEN") or os.environ.get("INGEST_TOKEN") or "").strip()


def fetch():
    """GET the Lab's library → (doc, None) or (None, reason). Never raises."""
    url = lab_url()
    if not url:
        return None, NOT_SET
    req = urllib.request.Request(url + "/api/boats/library",
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            raw = r.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        return None, f"Lab said {e.code}"
    except Exception as exc:  # noqa: BLE001 — offline, refused, timed out
        return None, f"Lab unreachable ({type(exc).__name__})"
    if len(raw) > MAX_BYTES:
        return None, "Boat library too large."
    try:
        return json.loads(raw.decode()), None
    except (ValueError, UnicodeDecodeError):
        return None, "Lab sent something that isn't a library."


def sync_once(now=None, fetcher=None):
    """One pull: skipped while racing (no shore call mid-race), else fetch + store. Records the
    outcome for GET /boats. Never raises."""
    now = time.time() if now is None else float(now)
    try:
        from . import racegate
        if racegate.racing():
            out = {"stored": False, "note": RACING}
        else:
            doc, why = (fetcher or fetch)()
            out = store(doc, now) if doc is not None else {"stored": False, "note": why}
    except Exception as exc:  # noqa: BLE001 — housekeeping never takes the engine down
        out = {"stored": False, "note": f"sync failed ({type(exc).__name__})"}
    _STATUS.update(at=now, ok=bool(out.get("stored")), note=out.get("note"))
    if out.get("dropped"):
        print(f"[boatlib] dropped: {out['dropped']}", flush=True)
    return out


def _loop():
    while True:
        out = sync_once()
        time.sleep(SYNC_S if out.get("stored") else RETRY_S)


def start_sync():
    """The background pull (engine startup). Off when LAB_URL is unset; says so on GET /boats."""
    if not lab_url():
        _STATUS.update(at=None, ok=False, note=NOT_SET)
        print(f"[boatlib] {NOT_SET} Library sync off.", flush=True)
        return False
    threading.Thread(target=_loop, name="boat-library", daemon=True).start()
    return True


# ---- which boat is being sailed -----------------------------------------------------------------

def _src():
    from . import datasource
    s = datasource.active()
    return s if hasattr(s, "session_current") else None


def _sessions():
    """(open session or None, last session boat or None), cached until a bump or SESSION_TTL_S."""
    now = time.monotonic()
    if _SESSION["gen"] == _GEN[0] and now - _SESSION["at"] < SESSION_TTL_S:
        return _SESSION["val"]
    cur = last = None
    s = _src()
    try:
        if s is not None:
            cur = s.session_current()
            if hasattr(s, "last_session_boat"):
                last = s.last_session_boat()
    except Exception:  # noqa: BLE001 — an unreadable store: fall back to the kit
        cur = last = None
    val = (cur, last)
    _SESSION.update(at=now, gen=_GEN[0], val=val)
    return val


_PUSH_MEMO = {"memo": None}      # (file key, pushed): the push file carries a chart (~1 MB)


def _pushed():
    """The Deploy push held aboard: {boat_id, profile, boat_version, polar, has_key, at} or None.
    Memoised on the file (the iPad polls /session every few seconds)."""
    from . import orinassets
    try:
        st = os.stat(orinassets._path())
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    memo = _PUSH_MEMO["memo"]
    if memo is not None and memo[0] == key:
        return memo[1]
    rec = orinassets._read()
    a = (rec or {}).get("assets") or {}
    boat = a.get("boat") if isinstance(a.get("boat"), dict) else None
    val = None if boat is None else {
        "boat_id": boat.get("boat_id"), "profile": boat, "boat_version": a.get("boat_version"),
        "polar": a.get("polar"), "has_key": "polar" in a,
        "at": float(rec.get("received_at") or 0)}
    _PUSH_MEMO["memo"] = (key, val)
    return val


def _name_of(bid, lib=None, pushed=None):
    lib = entries() if lib is None else lib
    p = (lib.get(bid) or {}).get("profile") or (
        (pushed or {}).get("profile") if (pushed or {}).get("boat_id") == bid else None)
    return (p or {}).get("name") or bid


def current():
    """{boat_id, name, from: session|last|deploy|kit, session_id}. boat_id None = an "Other boat"
    (a name only)."""
    cur, last = _sessions()
    lib, pushed = entries(), _pushed()
    if cur and (cur.get("boat_id") or cur.get("boat_name")):
        bid = cur.get("boat_id")
        return {"boat_id": bid, "name": cur.get("boat_name") or _name_of(bid, lib, pushed),
                "from": "session", "session_id": cur.get("id")}
    cands = []
    if last and (last.get("boat_id") or last.get("boat_name")):
        cands.append((float(last.get("start_ts") or 0), "last", last.get("boat_id"),
                      last.get("boat_name")))
    if pushed and pushed.get("boat_id"):
        cands.append((pushed["at"], "deploy", pushed["boat_id"], None))
    if cands:
        _at, frm, bid, name = max(cands, key=lambda c: c[0])
        return {"boat_id": bid, "name": name or _name_of(bid, lib, pushed), "from": frm,
                "session_id": None}
    k = kit_id()
    return {"boat_id": k, "name": _name_of(k, lib, pushed) if k else None, "from": "kit",
            "session_id": None}


def boat_for(bid):
    """Boat `bid`'s profile + polar aboard → {profile, boat_version, polar, from, at} (from:
    deploy | library | None). The Deploy push's copy when it is this boat's and newer than the
    library's; else the library's. An explicit "no polar" in the push gives way to the library
    (the push only meant the playbook's polar had changed); a push from a Lab before #304 (no
    polar key) gives the profile's inline cert. Never another boat's."""
    out = {"profile": None, "boat_version": None, "polar": None, "from": None, "at": None}
    if not bid:
        return out
    pushed = _pushed()
    lib = entries().get(bid)
    cand = []
    if pushed and pushed.get("boat_id") == bid:
        pol = pushed.get("polar")
        if not (isinstance(pol, dict) and not BP.check(pol, bid)):
            pol = BP.resolve(pushed["profile"], dirs=()) if not pushed["has_key"] else None
        cand.append({"profile": pushed["profile"], "boat_version": pushed["boat_version"],
                     "polar": pol, "from": "deploy", "at": pushed["at"]})
    if lib:
        cand.append({"profile": lib["profile"], "boat_version": lib["boat_version"],
                     "polar": lib.get("polar"), "from": "library",
                     "at": float(lib.get("got_at") or 0)})
    with_polar = [c for c in cand if c["polar"] is not None]
    pick = max(with_polar or cand, key=lambda c: (c["at"], c["from"] == "deploy"), default=None)
    return pick or out


_POLAR_MEMO = {"memo": None}


def _key():
    from . import orinassets
    keys = []
    for p in (orinassets._path(), _path()):
        try:
            st = os.stat(p)
            keys.append((st.st_ino, st.st_mtime_ns, st.st_size))
        except OSError:
            keys.append(None)
    return (tuple(keys), _GEN[0])


def current_boat():
    """The current boat with its profile and polar: current() + boat_for(). Memoised on the push
    file, the library file and the generation (a session change bumps it)."""
    key = _key()
    memo = _POLAR_MEMO["memo"]
    if memo is not None and memo[0] == key and time.monotonic() - memo[2] < SESSION_TTL_S:
        return memo[1]
    cur = current()
    b = boat_for(cur.get("boat_id"))
    val = dict(cur, profile=b["profile"], boat_version=b["boat_version"], polar=b["polar"],
               polar_from=b["from"] if b["polar"] is not None else None, boat_from=b["from"],
               at=b["at"])
    _POLAR_MEMO["memo"] = (key, val, time.monotonic())
    return val


def current_polar():
    """The current boat's polar block, or None ("No polar for this boat.")."""
    return current_boat().get("polar")


def resolve_pick(boat_id=None, boat_name=None):
    """The crew's pick → (boat_id or None, name). A boat the library (or the push) knows keeps its
    id and its profile's name. Anything else is an "Other boat": a name only, no polar. Nothing
    picked → the current boat (the last one used)."""
    bid = str(boat_id).strip()[:120] if isinstance(boat_id, str) and boat_id.strip() else None
    if bid == OTHER:
        bid = None
    name = clean_name(boat_name)
    if bid is None and name is None:
        cur = current()
        return cur.get("boat_id"), cur.get("name")
    known = set(entries())
    pushed = _pushed()
    if pushed and pushed.get("boat_id"):
        known.add(pushed["boat_id"])
    k = kit_id()
    if bid and (bid in known or bid == k):
        return bid, _name_of(bid, None, pushed)
    return None, name or clean_name(bid)


def playbook_boat():
    """The boat the deployed playbook was built for (its boat_model), else the push's, or None."""
    try:
        from . import deviation
        bid = ((deviation._load_playbook() or {}).get("boat_model") or {}).get("boat_id")
        if isinstance(bid, str) and bid:
            return bid
    except Exception:  # noqa: BLE001
        pass
    p = _pushed()
    return (p or {}).get("boat_id")


def view():
    """GET /boats: the picker's list, the current boat, the deployed playbook's boat, sync state."""
    lib = entries()
    pushed = _pushed()
    cur = current_boat()
    rows = [{"boat_id": bid, "name": (e.get("profile") or {}).get("name") or bid,
             "has_polar": e.get("polar") is not None} for bid, e in lib.items()]
    if pushed and pushed.get("boat_id") and pushed["boat_id"] not in lib:
        rows.append({"boat_id": pushed["boat_id"], "name": _name_of(pushed["boat_id"], lib, pushed),
                     "has_polar": boat_for(pushed["boat_id"])["polar"] is not None})
    rows.sort(key=lambda r: str(r["name"]).lower())
    pb = playbook_boat()
    doc = _read() or {}
    return {"current": {"boat_id": cur.get("boat_id"), "name": cur.get("name"),
                        "from": cur.get("from"), "has_polar": cur.get("polar") is not None,
                        "polar": BP.summary(cur.get("polar")), "polar_from": cur.get("polar_from"),
                        "note": None if cur.get("polar") is not None else NO_POLAR},
            "boats": rows, "kit": kit_id(),
            "playbook_boat": pb, "playbook_boat_name": _name_of(pb, lib, pushed) if pb else None,
            "library": {"synced_at": doc.get("synced_at"), "boats": len(lib),
                        "last_try": _STATUS.get("at"), "ok": _STATUS.get("ok"),
                        "note": _STATUS.get("note")}}
