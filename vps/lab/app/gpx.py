"""GPX export — put the frozen gameplan on the boat's CHARTPLOTTER (Garmin GPSMAP 943).

The 943 imports GPX user data from a memory card or the ActiveCaptain app and draws it on the
chart (Garmin "Importing User Data from a Third-Party Marine Device"). So the signed bundle's
optimal route becomes a real, visible path on the GPS screen — loaded dockside with the rest of
the homework, zero bus traffic, RRS-41 clean (pre-race frozen work on own equipment).

What we write (GPX 1.1, stdlib only):
  - a <wpt> per course mark (the rounding marks the crew actually navigates by);
  - a <rte> for the RECOMMENDED variant (navigable on the plotter: route points WP01…, with the
    plan's ETA at each point as <time> — the plotter shows the leg you should be on);
  - a <trk> per OTHER variant (drawn as lines for reference, without cluttering the route list) —
    and optionally for the recommended one too, since a track renders as a clean path.

Waypoint counts are bounded (Garmin routes cap at ~250 points; our paths run ~40-60) and names
are prefixed C4- so a re-import replaces cleanly instead of colliding with the crew's own marks.
"""
from __future__ import annotations

import time
from xml.sax.saxutils import escape


def _iso(t):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(t)))
    except (TypeError, ValueError, OverflowError):
        return None


def _pt(tag, p, name=None, desc=None, indent="  "):
    lat, lon = p.get("lat"), p.get("lon")
    if lat is None or lon is None:
        return ""
    out = f'{indent}<{tag} lat="{round(float(lat), 6)}" lon="{round(float(lon), 6)}">'
    t = _iso(p.get("t"))
    if t:
        out += f"<time>{t}</time>"
    if name:
        out += f"<name>{escape(str(name))}</name>"
    if desc:
        out += f"<desc>{escape(str(desc))}</desc>"
    return out + f"</{tag}>\n"


def _variant_label(v, i):
    return str(v.get("id") or v.get("name") or f"variant{i}")


def bundle_gpx(bundle: dict, marks=None, variants: str = "recommended") -> str:
    """GPX text for a frozen playbook bundle. `marks` = the course marks from the
    RaceDefinition (list of {name, lat, lon}); `variants` = 'recommended' | 'all'."""
    race = str(bundle.get("race_id") or "race")
    rec = str(bundle.get("recommended") or "")
    vs = bundle.get("variants") or []
    chosen = [v for v in vs if variants == "all" or _variant_label(v, 0) == rec] or vs[:1]

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<gpx version="1.1" creator="C4 Performance Lab" '
           'xmlns="http://www.topografix.com/GPX/1/1">',
           f"  <metadata><name>{escape('C4 gameplan — ' + race)}</name>"
           + (f"<time>{_iso(bundle.get('generated_at'))}</time>"
              if _iso(bundle.get("generated_at")) else "")
           + "</metadata>"]

    for m in (marks or []):
        out.append(_pt("wpt", m, name=f"C4-{m.get('name') or 'mark'}",
                       desc="course mark (frozen homework)"))

    for i, v in enumerate(chosen):
        vid = _variant_label(v, i)
        path = ((v.get("route") or {}).get("path")) or []
        if len(path) < 2:
            continue
        label = f"C4 {race} — {vid}" + (" (recommended)" if vid == rec else "")
        if vid == rec:
            # the recommended variant is the navigable ROUTE
            out.append(f"  <rte><name>{escape(label)}</name>"
                       f"<desc>{escape((v.get('summary') or '')[:250])}</desc>\n")
            for n, p in enumerate(path, 1):
                out.append(_pt("rtept", p, name=f"C4-{vid}-{n:02d}", indent="    "))
            out.append("  </rte>\n")
        # every chosen variant also renders as a TRACK — a clean drawn line on the chart
        out.append(f"  <trk><name>{escape(label)}</name><trkseg>\n")
        for p in path:
            out.append(_pt("trkpt", p, indent="    "))
        out.append("  </trkseg></trk>\n")

    out.append("</gpx>\n")
    return "".join(x if x.endswith("\n") else x + "\n" for x in out)
