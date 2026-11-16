"""A new boat profile from its ORC certificate.

The owner pastes what they have: the cert link (sas.orc.org/... — bot-blocked server-side, so it is
NEVER fetched; only the RefNo is read out of it), a bare RefNo, or a sail number. The record comes
from the public ORC country dump (`fleetimport._orc_dump`), the polar from its Allowances
(`orcpolar.cert_polar`), stored inline as the profile's `cert_polar` (shared/boat_polar.py source 2).

`lookup` finds the record and says which countries it searched. `preview` is what the Lab shows
before saving. `create` saves: a new boat_id from the yacht's name (never over a different boat),
or, when a profile already carries this RefNo, refreshes that profile's cert polar + facts.
"""
import re
import time

from shared import boat_profile, race_def
from . import boats, fleetimport, orcpolar

NOT_FOUND = "No ORC certificate for that number in the public list. Check the number or the country."
EMPTY = "Enter an ORC certificate link, number or sail number."
NO_REF_IN_LINK = "That link has no certificate number in it."
NO_POLAR = "This certificate has no speeds. Can't make a boat from it."
FALLBACK_COUNTRY = "USA"           # last country tried when nothing else names one

# ORC RefNo: 11 characters, 4 digits then letters/digits (e.g. 045600052L8)
_REF = re.compile(r"\b(\d{4}[0-9A-Z]{7})\b", re.I)


def parse_cert(text):
    """'<link>' | '<RefNo>' | '<sail number>' → {"ref": str|None, "sail": str|None}.
    A link only ever gives a RefNo (it is not fetched)."""
    t = str(text or "").strip()
    if not t:
        raise ValueError(EMPTY)
    if "://" in t or t.lower().startswith(("www.", "sas.orc.org", "data.orc.org")):
        m = _REF.search(t)
        if not m:
            raise ValueError(NO_REF_IN_LINK)
        return {"ref": m.group(1).upper(), "sail": None}
    m = _REF.fullmatch(t.replace(" ", ""))
    if m:
        # a bare 11-character RefNo; also tried as a sail number, in case it is one
        return {"ref": m.group(1).upper(), "sail": t}
    return {"ref": None, "sail": t}


def _ref_index(idx):
    """RefNo → record for one country's dump (`_orc_dump` builds it; an index without one gets it
    from its sail/name tables)."""
    by_ref = idx.get("by_ref")
    if by_ref is None:
        by_ref = {}
        for table in ("by_sail", "by_name"):
            for r in (idx.get(table) or {}).values():
                ref = str(r.get("RefNo") or "").strip().upper()
                if ref:
                    by_ref.setdefault(ref, r)
        idx["by_ref"] = by_ref
    return by_ref


def countries_to_search(parsed, country=None, definition=None, active=None):
    """Typed country, else the sail number's prefix, then the race's rating countries, the active
    boat's sail prefix, then USA. Order kept, no repeats."""
    out = []

    def add(c):
        c = str(c or "").strip().upper()
        if c and c not in out:
            out.append(c)

    for c in str(country or "").split(","):
        add(c)
    if not out:
        add(race_def.sail_prefix(parsed.get("sail")))
        for c in race_def.rating_countries(definition or {}, []) if definition else []:
            add(c)
        add(race_def.sail_prefix((active or {}).get("sail_number")))
        add(FALLBACK_COUNTRY)
    return out


def lookup(cert, country=None, definition=None, active=None):
    """Find the cert record. → {"ok": True, "rec", "country", "searched"} or
    {"ok": False, "note", "searched"}."""
    try:
        p = parse_cert(cert)
    except ValueError as e:
        return {"ok": False, "note": str(e), "searched": []}
    searched, failed = [], []
    for cc in countries_to_search(p, country, definition, active):
        try:
            idx = fleetimport._orc_dump(cc)
        except Exception as e:                      # one country down → try the next
            print(f"[certboat] ORC dump {cc} failed ({type(e).__name__})", flush=True)
            failed.append(cc)
            continue
        searched.append(cc)
        rec = None
        if p["ref"]:
            rec = _ref_index(idx).get(p["ref"])
        if rec is None and p["sail"]:
            rec = idx["by_sail"].get(fleetimport._norm_sail(p["sail"]))
        if rec is not None:
            return {"ok": True, "rec": rec, "country": cc, "searched": searched}
    note = NOT_FOUND
    if failed and not searched:
        note = "The ORC list didn't load. Try again in a minute."
    return {"ok": False, "note": note, "searched": searched, "failed": failed}


def _num(x):
    try:
        v = float(x)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _name(rec):
    n = str(rec.get("YachtName") or "").strip()
    return n.title() if n.isupper() else n


def facts(rec, country=None):
    """The cert facts a profile keeps so it says where its polar came from."""
    return {"ref_no": str(rec.get("RefNo") or "").strip().upper(),
            "cert_no": rec.get("CertNo") or "",
            "issue_date": str(rec.get("IssueDate") or "")[:10],
            "country": country or rec.get("NatAuth") or "",
            "gph": _num(rec.get("GPH"))}


def preview(rec, country=None):
    pol = orcpolar.cert_polar(rec)
    f = facts(rec, country)
    return {"name": _name(rec), "sail_number": rec.get("SailNo") or "",
            "boat_class": rec.get("Class") or "", "draft_m": _num(rec.get("Draft")),
            "loa_m": _num(rec.get("LOA")), "gph": f["gph"], "ref_no": f["ref_no"],
            "issue_date": f["issue_date"], "country": f["country"],
            "builder": rec.get("Builder") or "", "designer": rec.get("Designer") or "",
            "year": int(_num(rec.get("Age_Year"))) if _num(rec.get("Age_Year")) else None,
            "polar_rows": len(pol)}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")[:50]


def _find_by_ref(ref):
    for s in boats.list_boats():
        b = boats.get_boat(s.get("boat_id"))
        if b and ref and str(((b.get("provenance") or {}).get("cert") or {}).get("ref_no")
                             or "").upper() == ref:
            return b
    return None


def _new_id(rec):
    base = _slug(_name(rec)) or _slug(rec.get("SailNo")) or "boat"
    sail = _slug(rec.get("SailNo"))[:20]
    # ids stay within the profile's 60-character limit, suffix included
    tries = [base, f"{base[:59 - len(sail)]}-{sail}".strip("-")]
    base = base[:52]
    for bid in tries:
        if not boats.get_boat(bid):
            return bid
    n = 2
    while boats.get_boat(f"{base}-{n}"):
        n += 1
    return f"{base}-{n}"


def _cert_fields(rec, country):
    pol = orcpolar.cert_polar(rec)
    f = dict(facts(rec, country), source="ORC public certificate list",
             fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return pol, f


def create(rec, country=None):
    """Save a profile from cert record `rec`. → {"ok", "boat_id", "updated", "boat", "note"}."""
    pol, f = _cert_fields(rec, country)
    if not pol:
        return {"ok": False, "note": NO_POLAR}
    cert_polar = [list(r) for r in pol]
    issued = f" issued {f['issue_date']}" if f["issue_date"] else ""
    note = f"Polar and draft from ORC certificate {f['ref_no']}{issued}."
    old = _find_by_ref(f["ref_no"])
    if old:
        old["cert_polar"] = cert_polar
        old["orc"] = dict(old.get("orc") or {}, gph=f["gph"])
        old["provenance"] = dict(old.get("provenance") or {}, cert=f)
        boats.save_boat(old)
        return {"ok": True, "boat_id": old["boat_id"], "updated": True, "boat": old,
                "note": f"{old.get('name') or old['boat_id']} already has this certificate. "
                        "Its speeds are updated."}
    draft = _num(rec.get("Draft"))
    if draft is None:
        return {"ok": False, "note": "This certificate has no draft. Can't make a boat from it."}
    b = {"schema_version": boat_profile.SCHEMA_VERSION, "boat_id": _new_id(rec),
         "name": _name(rec) or str(rec.get("SailNo") or ""), "boat_class": rec.get("Class") or "",
         "sail_number": rec.get("SailNo") or "", "draft_m": round(draft, 4),
         "safety_margin_m": boat_profile.DEFAULT_MARGIN_M,
         "orc": {"rating": None, "gph": f["gph"], "scoring": ""},
         "beam_m": _num(rec.get("MB")), "air_draft_m": None,
         "displacement_kg": _num(rec.get("Dspl_Measurement")), "hull_type": "mono",
         "loa_m": _num(rec.get("LOA")), "sail_inventory": [], "jib_crossovers": [], "code0": {},
         "main_reefs": {}, "helm_factor": 1.0, "polar_adjustments": [], "wave_coeffs": {},
         "cert_polar": cert_polar, "note": note,
         "provenance": {"source": "ORC certificate", "review_status": "new", "cert": f}}
    errs, _w = boat_profile.validate(b)
    errs += boat_profile.shape_errors(b)
    if errs:
        return {"ok": False, "note": "Can't make a boat from this certificate: " + errs[0]}
    boats.save_boat(b)
    return {"ok": True, "boat_id": b["boat_id"], "updated": False, "boat": b,
            "note": f"{b['name']} added."}


# ---- a boat profile → the race's "Our boat" ------------------------------------------------------
OWN_NUMBERS = ("orc_gph", "rating", "rating_col", "tod_allowance", "tod_col")
OWN_NO_LIST = "The ORC list didn't load. GPH is from the boat; type the ToT."


def _same_boat(a, b):
    """Same boat when the sail numbers match, else (no sail on one side) the names."""
    sa, sb = fleetimport._norm_sail(a.get("sail")), fleetimport._norm_sail(b.get("sail"))
    if sa and sb:
        return sa == sb
    return fleetimport._norm(a.get("boat")) == fleetimport._norm(b.get("boat"))


def own_from_boat(boat, definition):
    """The race's `own` filled from a boat profile. → (own, note|None).

    Name and sail come from the profile; division and MMSI stay as the race has them (MMSI from
    the profile when it has one). With an ORC cert (`provenance.cert.ref_no`) the cert record is
    looked up again and gives GPH plus the ToT (and ToD) for the race's columns for our boat.
    Numbers typed for another boat are dropped; for the same boat without a cert they stay."""
    d = definition or {}
    old = dict(d.get("own") or {})
    own = dict(old)
    own["boat"] = str(boat.get("name") or boat.get("boat_id") or "")
    if boat.get("sail_number"):
        own["sail"] = str(boat["sail_number"])
    if str(boat.get("mmsi") or "").strip():
        own["mmsi"] = str(boat["mmsi"]).strip()
    if not _same_boat(old, own):
        for k in OWN_NUMBERS:
            own.pop(k, None)
    cert = (boat.get("provenance") or {}).get("cert") or {}
    ref = str(cert.get("ref_no") or "").strip().upper()
    gph = _num(cert.get("gph")) or _num((boat.get("orc") or {}).get("gph"))
    if not ref:
        if gph is not None:
            own["orc_gph"] = gph
        return own, None
    found = lookup(ref, country=cert.get("country") or None, definition=d)
    if not found["ok"]:
        if gph is not None:
            own["orc_gph"] = gph
        for k in ("rating", "rating_col", "tod_allowance", "tod_col"):
            own.pop(k, None)
        return own, OWN_NO_LIST if found.get("failed") and not found["searched"] else found["note"]
    tot_col, tod_col = fleetimport._cols(d, own)
    fleetimport._set_numbers(own, found["rec"], tot_col, tod_col)
    if own.get("orc_gph") is None and gph is not None:
        own["orc_gph"] = gph
    note = None
    if tot_col and own.get("rating") is None:
        note = f"This certificate has no {tot_col}. Type the ToT."
    elif not tot_col:
        note = "The race has no rating column yet. Set it in Rules."
    return own, note
