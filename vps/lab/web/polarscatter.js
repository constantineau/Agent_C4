/* Log points behind the observed polar (#376).
   The Polar tab draws one point per cell: the best speed held (p80). Behind it this draws the
   samples the cell came from, thinned server-side (app/polarscatter.py, `scatter` in
   /api/polar/observed), under the SAME race and sail filters, coloured by wind band. It only
   shows evidence; the polar still changes only through suggest → approve (Learnings).

   Hook for other point sets (e.g. saved debrief tests, #374): call
     polRegisterPointSource(name, (ctx) => [{tws, twa, stw, label?, config?, recording?}, ...])
   Those draw as hollow diamonds over the log points, under the same filters. Nothing here needs
   them to exist. Uses app.js globals at paint time (Pol, polSameRec, esc). */
"use strict";

const PolSc = { mode: "tws", sources: [], _of: null, _rows: null };
// Wind bands are ordered, so a sequential ramp (light wind green → strong wind magenta). Mid
// tones only, so the dots read on both the light and the dark panel.
const POL_SC_RAMP = ["#8fd18a", "#4fb8a0", "#2f9bb5", "#3f7cc4", "#6a5fc4", "#8f45b0", "#b0308f"];
const POL_SC_MODES = [["off", "off"], ["tws", "this wind"], ["all", "all winds"]];

// rows of the packed block as objects, memoised on the artifact
function polScRows(d) {
  const sc = (d || {}).scatter;
  if (!sc || !Array.isArray(sc.rows)) return [];
  if (PolSc._of === sc) return PolSc._rows;
  const f = sc.fields || [];
  PolSc._rows = sc.rows.map((r) => { const o = {}; f.forEach((k, i) => { o[k] = r[i]; }); return o; });
  PolSc._of = sc;
  return PolSc._rows;
}

// The polar cell's TWS band. Log points carry the band the build filed them under (`band`), so
// the page never re-derives it; for anything else, round half to EVEN like the build's Python
// (9 kn is the 8 kn cell there — Math.round would say 10).
function polScRoundEven(x) {
  const f = Math.floor(x), d = x - f;
  if (Math.abs(d - 0.5) < 1e-9) return f % 2 === 0 ? f : f + 1;
  return Math.round(x);
}
function polScBand(r, step) {
  return r.band != null ? r.band : polScRoundEven(r.tws / step) * step;
}

// The cells the polar on screen actually shows (they passed the seconds gate), keyed like the
// view: pooled cells for all races, that race's cells for one race, the race curves when lines
// are by race. A dot whose cell was refused is not drawn — it would be evidence for a point the
// chart says it doesn't have.
function polScRecKey(rec) { return rec == null ? "" : String(Math.round(Number(rec))); }
function polScCellKey(r, ctx) {
  const b = r.band != null ? r.band : r.tws, a = r.twa_bin != null ? r.twa_bin : r.twa;
  if (ctx.split === "race") return `${b}|${a}|${polScRecKey(r.recording)}`;
  if (ctx.race === "all" || ctx.race == null) return `${b}|${a}|${r.config || ""}`;
  return `${b}|${a}|${r.config || ""}|${polScRecKey(r.recording)}`;
}
function polScAccepted(data, ctx) {
  const d = data || {};
  const src = ctx.split === "race" ? d.race_curves
    : (ctx.race === "all" || ctx.race == null) ? d.cells : d.race_cells;
  return new Set((src || []).map((c) => polScCellKey(c, ctx)));
}

function polScSame(a, b) {
  return a != null && b != null && Math.abs(Number(a) - Number(b)) < 1;
}

// ctx: {mode, tws, race, config, split, step}. The sail filter is off when lines are by race,
// like the lines themselves.
function polScFilter(rows, ctx) {
  if (!ctx || ctx.mode === "off") return [];
  const step = ctx.step || 2;
  return rows.filter((r) => r.tws != null && r.twa != null && r.stw != null
    && (ctx.mode !== "tws" || polScBand(r, step) === ctx.tws)
    && (ctx.race === "all" || ctx.race == null || polScSame(r.recording, ctx.race))
    && (ctx.split === "race" || ctx.config === "all" || ctx.config == null
        || (r.config || "—") === ctx.config));
}

// every band that has points, ascending — the ramp is spread over these, so a colour means the
// same wind whatever the filter
function polScBands(rows, step) {
  return [...new Set(rows.filter((r) => r.tws != null).map((r) => polScBand(r, step)))]
    .sort((a, b) => a - b);
}

function polScColor(band, bands) {
  const i = bands.indexOf(band);
  if (i < 0) return "#9a9aa6";
  const j = bands.length < 2 ? Math.floor(POL_SC_RAMP.length / 2)
    : Math.round(i * (POL_SC_RAMP.length - 1) / (bands.length - 1));
  return POL_SC_RAMP[j];
}

function polScCtx() {
  const d = (typeof Pol !== "undefined" && Pol.data) || {};
  return { mode: PolSc.mode, tws: Pol.tws, race: Pol.race, config: Pol.config, split: Pol.split,
           step: (d.grid || {}).tws_step_kn || 2 };
}

// The SVG behind the polar lines. XY(twa, kn) -> [x, y] is the chart's own projection; points
// past the outer ring are left off (the scale never moves for them) and counted.
function polScSvg(XY, maxV, ctx, data) {
  ctx = ctx || polScCtx();
  data = data || (typeof Pol !== "undefined" ? Pol.data : null);
  const all = polScRows(data);
  const bands = polScBands(all, ctx.step);
  const ok = polScAccepted(data, ctx);
  const pts = polScFilter(all, ctx).filter((p) => ok.has(polScCellKey(p, ctx)));
  let off = 0;
  const out = [];
  for (const p of pts) {
    if (p.stw > maxV) { off++; continue; }
    const [x, y] = XY(p.twa, p.stw);
    out.push(`<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="1.8" fill="${polScColor(polScBand(p, ctx.step), bands)}"/>`);
  }
  const extra = [];
  for (const s of PolSc.sources) {
    let got = [];
    try { got = polScFilter((s.fn(ctx) || []), ctx.mode === "off" ? { ...ctx, mode: "tws" } : ctx); }
    catch (e) { got = []; }
    for (const p of got) {
      if (p.stw > maxV) continue;
      const [x, y] = XY(p.twa, p.stw);
      const lab = p.label || s.name;
      extra.push(`<path d="M ${x.toFixed(1)} ${(y - 5).toFixed(1)} l 5 5 l -5 5 l -5 -5 Z" fill="none" stroke="var(--ink)" stroke-width="1.5"><title>${typeof esc === "function" ? esc(lab) : lab}</title></path>`);
    }
  }
  PolSc.last = { shown: out.length, off, extra: extra.length };
  return `<g class="pol-scatter" opacity="0.5" pointer-events="none">${out.join("")}</g>`
    + (extra.length ? `<g class="pol-extra">${extra.join("")}</g>` : "");
}

function polScSetMode(m) { PolSc.mode = m; if (typeof paintPolar === "function") paintPolar(); }

function polScControls() {
  return `<span class="muted">log points:</span> ` + POL_SC_MODES.map(([k, lab]) =>
    `<label style="cursor:pointer;margin-right:8px"><input type="radio" name="polsc" value="${k}" ${PolSc.mode === k ? "checked" : ""}
      onchange="polScSetMode('${k}')"> ${lab}</label>`).join("");
}

// One line under the chart: what the dots are, how many, and the wind colours when it matters.
function polScLegend(ctx, data) {
  ctx = ctx || polScCtx();
  data = data || (typeof Pol !== "undefined" ? Pol.data : null);
  const sc = (data || {}).scatter;
  if (ctx.mode === "off" || !sc) return "";
  const all = polScRows(data);
  const bands = polScBands(all, ctx.step);
  const last = PolSc.last || { shown: 0, off: 0, extra: 0 };
  const dot = (c) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c};margin-right:3px;opacity:.7"></span>`;
  const keys = ctx.mode === "all"
    ? bands.map((b) => `<span style="white-space:nowrap">${dot(polScColor(b, bands))}${b} kn</span>`).join(" ")
    : `<span style="white-space:nowrap">${dot(polScColor(ctx.tws, bands))}${ctx.tws} kn</span>`;
  const thinned = sc.total > sc.kept ? ` A sample of ${sc.kept} from ${sc.total}.` : "";
  const off = last.off ? ` ${last.off} faster than the chart, not drawn.` : "";
  const extra = last.extra ? ` ◇ = saved test.` : "";
  return `<div class="muted" style="font-size:11px;margin-top:4px">${keys} Dots are logged speeds (${last.shown} shown), same filters.${thinned}${off}${extra}</div>`;
}

// The hook for other point sets (saved debrief tests, #374). Re-registering a name replaces it.
function polRegisterPointSource(name, fn) {
  PolSc.sources = PolSc.sources.filter((s) => s.name !== name);
  if (typeof fn === "function") PolSc.sources.push({ name, fn });
}
