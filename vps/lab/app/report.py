"""Gameplan → PDF report (reportlab).

A curated, shareable pre-race document built from the optimizer result + the (optional) branching
playbook — the crew's "email the gameplan" artifact (the richer sibling of the leg-table CSV export).
Pure-python reportlab (no system libs). Defensive: every section is optional and self-contained, so a
missing playbook / model-skill / route geometry just drops that section rather than failing the render.
"""
import datetime
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (Flowable, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

_NAVY = colors.HexColor("#0b2b45")
_TEAL = colors.HexColor("#1f7a8c")
_GREY = colors.HexColor("#5a6b78")
_LINE = colors.HexColor("#c8d3db")


def _styles():
    ss = getSampleStyleSheet()
    out = {
        "title": ParagraphStyle("t", parent=ss["Title"], textColor=_NAVY, fontSize=20, spaceAfter=2),
        "sub": ParagraphStyle("s", parent=ss["Normal"], textColor=_GREY, fontSize=10, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], textColor=_TEAL, fontSize=13,
                             spaceBefore=12, spaceAfter=5),
        "body": ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=13),
        "small": ParagraphStyle("sm", parent=ss["Normal"], fontSize=8, textColor=_GREY, leading=10),
        "mono": ParagraphStyle("m", parent=ss["Code"], fontSize=8.5, leading=12,
                               textColor=colors.HexColor("#1a2b38")),
        "cell": ParagraphStyle("c", parent=ss["Normal"], fontSize=8.5, leading=11),
        "cellb": ParagraphStyle("cb", parent=ss["Normal"], fontSize=8.5, leading=11, fontName="Helvetica-Bold"),
    }
    return out


def _esc(v):
    return ("" if v is None else str(v)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _fmt(v, dash="—"):
    return dash if v is None else v


class RouteMap(Flowable):
    """A simple, undistorted route schematic: the optimal-route polyline + the course marks, scaled
    into a box (longitude compressed by cos(lat) so it's not stretched). Never raises — an empty/bad
    geometry just draws nothing."""

    def __init__(self, path, marks, width, height):
        super().__init__()
        self.path = path or []
        self.marks = marks or []
        self.width = width
        self.height = height

    def wrap(self, *_):
        return (self.width, self.height)

    def draw(self):
        import math
        c = self.canv
        pts = [(p.get("lon"), p.get("lat")) for p in self.path if p.get("lat") is not None]
        mk = [(m.get("lon"), m.get("lat"), m.get("name")) for m in self.marks if m.get("lat") is not None]
        allpts = [(x, y) for x, y in pts] + [(x, y) for x, y, _ in mk]
        if len(allpts) < 2:
            return
        lons = [x for x, y in allpts]
        lats = [y for x, y in allpts]
        mlat = sum(lats) / len(lats)
        kx = math.cos(math.radians(mlat)) or 1e-6           # lon->distance compression
        xs = [x * kx for x in lons]
        minx, maxx, miny, maxy = min(xs), max(xs), min(lats), max(lats)
        pad = 16
        w, h = self.width - 2 * pad, self.height - 2 * pad
        spanx, spany = (maxx - minx) or 1e-6, (maxy - miny) or 1e-6
        scale = min(w / spanx, h / spany)
        ox = pad + (w - spanx * scale) / 2
        oy = pad + (h - spany * scale) / 2

        def X(lon):
            return ox + (lon * kx - minx) * scale

        def Y(lat):
            return oy + (lat - miny) * scale

        # frame
        c.setStrokeColor(_LINE)
        c.setLineWidth(0.5)
        c.rect(0, 0, self.width, self.height)
        # route
        if len(pts) >= 2:
            c.setStrokeColor(_TEAL)
            c.setLineWidth(1.4)
            p = c.beginPath()
            p.moveTo(X(pts[0][0]), Y(pts[0][1]))
            for lon, lat in pts[1:]:
                p.lineTo(X(lon), Y(lat))
            c.drawPath(p)
        # marks
        c.setFont("Helvetica", 7)
        for lon, lat, name in mk:
            c.setFillColor(_NAVY)
            c.circle(X(lon), Y(lat), 2.4, fill=1, stroke=0)
            if name:
                c.setFillColor(_GREY)
                c.drawString(X(lon) + 4, Y(lat) - 2, _esc(name)[:18])
        # start / finish dots
        if len(pts) >= 2:
            c.setFillColor(colors.HexColor("#2e7d32"))
            c.circle(X(pts[0][0]), Y(pts[0][1]), 3, fill=1, stroke=0)
            c.setFillColor(colors.HexColor("#c62828"))
            c.circle(X(pts[-1][0]), Y(pts[-1][1]), 3, fill=1, stroke=0)
        # north arrow
        c.setStrokeColor(_GREY)
        c.setFillColor(_GREY)
        c.setLineWidth(0.8)
        nx, ny = self.width - 16, self.height - 26
        c.line(nx, ny, nx, ny + 14)
        c.line(nx, ny + 14, nx - 3, ny + 9)
        c.line(nx, ny + 14, nx + 3, ny + 9)
        c.setFont("Helvetica", 6)
        c.drawCentredString(nx, ny - 8, "N")


def _kv_table(rows, styles):
    """A three-pair (value,label) stat grid (a compact summary block)."""
    data = [[Paragraph(f"<b>{_esc(v)}</b>", styles["cellb"]), Paragraph(_esc(k), styles["small"])]
            for k, v in rows]
    # lay out as a 3-wide grid of (value,label) pairs
    flat, row = [], []
    for cell in data:
        row.append(cell[0])
        row.append(cell[1])
        if len(row) == 6:
            flat.append(row)
            row = []
    if row:
        row += [""] * (6 - len(row))
        flat.append(row)
    t = Table(flat, colWidths=[x * inch for x in (0.85, 1.4, 0.85, 1.4, 0.85, 1.4)])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                           ("TOPPADDING", (0, 0), (-1, -1), 2),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    return t


def build_gameplan_pdf(payload: dict) -> bytes:
    r = payload.get("result") or {}
    pb = payload.get("playbook") or {}
    st = _styles()
    story = []
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ---- header ----
    race = payload.get("race_name") or r.get("race_id") or "Gameplan"
    story.append(Paragraph(f"Gameplan — {_esc(race)}", st["title"]))
    meta = " · ".join(x for x in [
        f"Course: {_esc(r.get('course_id'))}" if r.get("course_id") else None,
        f"Boat: {_esc(payload.get('boat'))}" if payload.get("boat") else None,
        f"Generated {now}"] if x)
    story.append(Paragraph(meta, st["sub"]))

    if not r.get("available", True) or not r.get("legs"):
        story.append(Paragraph("No optimized route in this gameplan yet — run the optimizer first.",
                               st["body"]))
        return _render(story)

    # ---- live route player (share link + QR) ----
    if payload.get("share_url"):
        try:
            from reportlab.graphics.barcode import qr as _qr
            from reportlab.graphics.shapes import Drawing
            url = str(payload["share_url"])
            widget = _qr.QrCodeWidget(url, barLevel="M")
            x1, y1, x2, y2 = widget.getBounds()
            side = 0.95 * inch
            drawing = Drawing(side, side,
                              transform=[side / (x2 - x1), 0, 0, side / (y2 - y1), 0, 0])
            drawing.add(widget)
            blurb = Paragraph(
                "<b>Interactive route player</b> — the playable version of this gameplan "
                "(boat animating along the route through the forecast). Scan the code or open:"
                f'<br/><link href="{_esc(url)}"><u>{_esc(url)}</u></link>', st["body"])
            t = Table([[blurb, drawing]], colWidths=[5.5 * inch, 1.1 * inch])
            t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("BOX", (0, 0), (-1, -1), 0.6, _TEAL),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 8),
                                   ("TOPPADDING", (0, 0), (-1, -1), 6),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
            story.append(t)
            story.append(Spacer(1, 6))
        except Exception:
            pass   # the QR is decoration — a barcode hiccup must never sink the report

    # ---- route summary ----
    story.append(Paragraph("Route summary", st["h2"]))
    rl = r.get("realized") or {}
    cur = r.get("current") or {}
    rows = [("hours", _fmt(r.get("total_hours"))), ("nm sailed", _fmt(r.get("total_sailed_nm"))),
            ("nm direct", _fmt(r.get("total_direct_nm"))), ("tacks/gybes", _fmt(r.get("total_tacks")))]
    if r.get("total_peels") is not None:
        rows.append(("sail peels", r.get("total_peels")))
    rows.append(("route conf", _fmt(r.get("route_confidence"))))
    if r.get("wind_coverage") is not None:
        rows.append(("wind cov", f"{round(r['wind_coverage'] * 100)}%"))
    if rl.get("realized_pct") is not None:
        rows.append(("realized %", f"{rl['realized_pct']}%"))
    if rl.get("sea_state_hs_mean"):
        rows.append(("sea state", f"{rl['sea_state_hs_mean']} m"))
    if cur.get("source"):
        rows.append(("current", _esc(cur.get("source"))))
    story.append(_kv_table(rows, st))

    # ---- route schematic ----
    if r.get("path"):
        try:
            story.append(Spacer(1, 6))
            story.append(RouteMap(r.get("path"), r.get("marks"), 6.5 * inch, 3.2 * inch))
        except Exception:
            pass

    # ---- roundings ----
    if r.get("roundings"):
        story.append(Paragraph("Required roundings", st["h2"]))
        rnd = r["roundings"]
        if isinstance(rnd, list):
            txt = "; ".join(_esc(x.get("text") or f"{x.get('name')} to {x.get('side')}"
                                 if isinstance(x, dict) else x) for x in rnd)
        else:
            txt = _esc(rnd)
        story.append(Paragraph(txt, st["body"]))

    # ---- legs ----
    story.append(Paragraph("Leg-by-leg", st["h2"]))
    head = ["#", "To", "Min", "Point of sail", "Sail", "Tacks", "TWS", "TWD"]
    data = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in head]]
    for i, l in enumerate(r["legs"], 1):
        w = l.get("wind") or {}
        data.append([
            Paragraph(str(i), st["cell"]), Paragraph(_esc(l.get("to")), st["cell"]),
            Paragraph(_esc(l.get("leg_minutes")), st["cell"]),
            Paragraph(_esc(l.get("point_of_sail")), st["cell"]),
            Paragraph(_esc(l.get("sail")) + (f" (+{l['peels']} peel)" if l.get("peels") else ""), st["cell"]),
            Paragraph(_esc(l.get("tacks") or 0), st["cell"]),
            Paragraph(_esc(w.get("tws")), st["cell"]),
            Paragraph(f"{_esc(w.get('twd'))}°" if w.get("twd") is not None else "—", st["cell"])])
    t = Table(data, colWidths=[x * inch for x in (0.3, 1.3, 0.5, 1.2, 1.0, 0.6, 0.6, 0.7)], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), _NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6f8")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story.append(t)

    # ---- briefing ----
    if r.get("briefing"):
        story.append(Paragraph("Pre-race briefing", st["h2"]))
        for para in str(r["briefing"]).split("\n"):
            if para.strip():
                story.append(Paragraph(_esc(para), st["body"]))
                story.append(Spacer(1, 3))

    # ---- branching playbook ----
    if pb.get("variants"):
        story.append(Paragraph("Branching playbook", st["h2"]))
        if pb.get("headline"):
            story.append(Paragraph(f"<b>{_esc(pb['headline'])}</b>", st["body"]))
        recid = pb.get("recommended")
        recname = next((v.get("name") for v in pb["variants"] if v.get("id") == recid), recid)
        bits = [f"Recommended start: <b>{_esc(recname)}</b>"]
        if pb.get("decision_spread_min") is not None:
            bits.append(f"decision stakes ~{round(pb['decision_spread_min'])} min")
        story.append(Paragraph(" · ".join(bits), st["small"]))
        story.append(Spacer(1, 4))
        for v in pb["variants"]:
            star = "★ " if v.get("id") == recid else ""
            share = f" ({round(v['share'] * 100)}% of models)" if isinstance(v.get("share"), (int, float)) else ""
            story.append(Paragraph(f"{star}<b>{_esc(v.get('name') or v.get('id'))}</b>{share}", st["body"]))
            for label, key in [("", "summary"), ("Why", "rationale"),
                               ("Trade-offs", "tradeoffs"), ("Switch when", "what_flips_it")]:
                val = v.get(key)
                if val:
                    prefix = f"<b>{label}:</b> " if label else ""
                    story.append(Paragraph(prefix + _esc(val), st["cell"]))
            story.append(Spacer(1, 6))
        if pb.get("decision_tree"):
            story.append(Paragraph("Decision tree (from the gun)", st["h2"]))
            for i, step in enumerate(pb["decision_tree"], 1):
                if isinstance(step, dict):
                    line = f"{i}. <b>Observe</b> {_esc(step.get('observe'))} → <b>{_esc(step.get('action'))}</b>"
                    if step.get("variant"):
                        line += f" ({_esc(step.get('variant'))})"
                else:
                    line = f"{i}. {_esc(step)}"
                story.append(Paragraph(line, st["cell"]))

    # ---- model skill ----
    ms = r.get("model_skill")
    if isinstance(ms, dict) and ms.get("models"):
        story.append(Paragraph("Weather-model skill (venue backtest)", st["h2"]))
        data = [[Paragraph(f"<b>{h}</b>", st["cell"]) for h in ["Model", "RMSE (kn)", "Weight", "Bias °", "n"]]]
        for m in ms["models"]:
            data.append([Paragraph(_esc(m.get("model", "").upper()), st["cell"]),
                         Paragraph(_esc(m.get("rmse_kn")), st["cell"]),
                         Paragraph(_esc(m.get("weight")), st["cell"]),
                         Paragraph(_esc(m.get("dir_bias_deg")), st["cell"]),
                         Paragraph(_esc(m.get("n")), st["cell"])])
        t = Table(data, colWidths=[x * inch for x in (1.4, 1.0, 1.0, 0.9, 0.8)], repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), _NAVY),
                               ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                               ("GRID", (0, 0), (-1, -1), 0.4, _LINE),
                               ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
        story.append(t)

    story.append(Spacer(1, 14))
    story.append(Paragraph(
        "Pre-race cloud homework — frozen at the gun (RRS 41). Routes optimize on the SR33 polars "
        "through a multi-model GRIB forecast; confidence = model agreement. Verify against the "
        "official course &amp; the chart before racing.", st["small"]))
    return _render(story)


def _render(story) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch, title="C4 Gameplan")
    doc.build(story)
    return buf.getvalue()
