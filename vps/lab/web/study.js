/* Gameplan "Study" batch (#378): route the course many times, one thing changed each time, and
   compare. Server: app/study.py, POST /api/study (runs in the optimize job slot; polled on
   /api/optimize/status). Uses app.js helpers at run time (Opt, apiGet, apiPost, esc, qhelp,
   optStartEpoch, optTz, renderGameplan, jobWebVersion). The fan is drawn by MapView on the
   Gameplan map: the study's baseline route with every run as a coloured line. */
"use strict";

const Study = (function () {
  const MAX_RUNS = 40;                       // the server's hard ceiling (study.MAX_RUNS)
  const LIMITS = { start_min: [-720, 720, 0], polar_pct: [50, 150, 100],
    tws_pct: [50, 200, 100], twd_deg: [-90, 90, 0] };
  // rough seconds per run by Detail (the route's own time budget is 60 / 90 / 200 s), + weather once
  const RUN_S = { fast: 30, auto: 45, fine: 100 };
  const WEATHER_S = 60;
  const AXIS_COLORS = { member: "#9467bd", start_min: "#17becf", polar_pct: "#ff7f0e",
    tws_pct: "#8c564b", twd_deg: "#e377c2" };
  const MODEL_COLORS = { gfs: "#1f77b4", hrrr: "#d62728", rrfs: "#2ca02c", gefs: "#9467bd",
    ecmwf: "#ff7f0e", "ecmwf-ens": "#8c564b", icon: "#e377c2", gem: "#bcbd22" };

  const S = { models: true, members: false, start_min: "-30, 30", polar_pct: "95, 105",
    tws_pct: "90, 110", twd_deg: "-10, 10", cap: 24, running: false, result: null, err: "" };

  // "-30, 0, 30" → [-30, 30]: numbers inside the axis limits, the baseline value dropped (as the server)
  function parseList(text, key) {
    const [lo, hi, base] = LIMITS[key];
    const out = new Set();
    String(text || "").replace(/,/g, " ").split(/\s+/).forEach((t) => {
      if (t === "") return;
      const v = Math.round(Number(t));
      if (Number.isFinite(v) && v >= lo && v <= hi && v !== base) out.add(v);
    });
    return [...out].sort((a, b) => a - b);
  }

  function axesOf(s) {
    const a = { models: !!s.models, members: !!s.members };
    for (const k of Object.keys(LIMITS)) a[k] = parseList(s[k], k);
    return a;
  }

  // runs in a plan: the baseline + each value on each axis (one thing at a time), as study.run_count
  function runCount(axes, nModels, nMembers) {
    let n = 1;
    if (axes.models && nModels >= 2) n += nModels;
    if (axes.members && nMembers >= 1) n += nMembers;
    for (const k of Object.keys(LIMITS)) n += (axes[k] || []).length;
    return n;
  }

  function estimateMin(n, resolution) {
    return Math.max(1, Math.ceil((WEATHER_S + n * (RUN_S[resolution] || RUN_S.auto)) / 60));
  }

  function colorOf(row) {
    if (row.axis === "model") return MODEL_COLORS[row.model] || "#7a7a7a";
    if (row.axis === "twd_deg") return row.twd_deg > 0 ? "#d62728" : "#1f77b4";
    return AXIS_COLORS[row.axis] || "#7a7a7a";
  }

  function fmtClock(epoch, tz) {
    if (epoch == null) return "—";
    try {
      return new Intl.DateTimeFormat("en-GB", { timeZone: tz || undefined, weekday: "short",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(epoch * 1000));
    } catch (e) { return new Date(epoch * 1000).toISOString().slice(11, 16) + "Z"; }
  }

  function fmtDelta(m) {
    if (m == null) return "—";
    if (Math.abs(m) < 0.5) return "0";
    return (m > 0 ? "+" : "−") + Math.round(Math.abs(m)) + " min";
  }

  function sidesText(sides) {
    const parts = ["right", "left", "middle"].filter((k) => sides && sides[k])
      .map((k) => `${sides[k]} ${k}`);
    return parts.length ? "Start: " + parts.join(", ") : "";
  }

  // what a model or ensemble run alone was: cycle, blend weight, any manual correction (#380)
  function sourceText(row) {
    if (row.axis !== "model" && row.axis !== "member") return "";
    const bits = [];
    if (row.cycle) bits.push(row.cycle);
    if (row.weight_pct != null) bits.push(`${row.weight_pct}% of blend`);
    if (row.correction) bits.push("corrected: " + row.correction);
    return bits.join(" · ");
  }

  // the manual model corrections every run used (#384), in words
  function corrText(r) {
    const c = r && r.windfield && r.windfield.corrections;
    return c && c.text && c.text.length ? c.text.join("; ") : "";
  }

  function rowHtml(row, esc, tz) {
    const sw = `<i class="st-sw" style="background:${row.id === "base" ? "var(--ink)" : colorOf(row)}"></i>`;
    if (!row.ok) {
      return `<tr class="st-fail"><td>${sw}${esc(row.label)}</td><td colspan="4" class="muted">${esc(row.note || "No route.")}</td>
        <td class="muted">${esc(sourceText(row))}</td></tr>`;
    }
    const tie = row.tie && row.id !== "base" ? ' <span class="pill st-tie">tie</span>' : "";
    return `<tr><td>${sw}${esc(row.label)}${row.degraded ? ' <span class="pill warn" title="Ran out of wind data or time">rough</span>' : ""}</td>
      <td>${esc(fmtClock(row.finish_epoch, tz))}</td><td>${row.hours != null ? row.hours + " h" : "—"}</td>
      <td>${esc(fmtDelta(row.delta_min))}${tie}</td><td>${esc(row.side || "—")}</td>
      <td class="muted">${esc(sourceText(row))}</td></tr>`;
  }

  // the results table + summary; `esc` passed in (app.js's) so node tests can load this alone
  function resultsHtml(r, esc, tz) {
    if (!r) return "";
    if (!r.available) return `<div class="placeholder">Study not run: ${esc(r.note || "unavailable")}</div>`;
    const spread = r.spread_min != null ? `Finish spread ${Math.round(r.spread_min)} min.` : "";
    return `<div class="st-sum"><b>${r.finished} of ${r.rows.length} runs finished.</b> ${esc(spread)}
        ${esc(sidesText(r.sides))}</div>
      ${corrText(r) ? `<div class="muted st-note">Corrected: ${esc(corrText(r))}</div>` : ""}
      <div class="muted st-note">${esc(r.agreement_note || "")}</div>
      <div class="muted st-note">${esc(r.tie_note || "")}</div>
      <div class="st-wrap"><table class="legs st-table"><thead><tr><th>Run</th><th>Finish</th><th>Time</th>
        <th title="Time on the course vs the baseline">vs base</th><th>Side</th><th>Forecast</th></tr></thead>
      <tbody>${r.rows.map((row) => rowHtml(row, esc, tz)).join("")}</tbody></table></div>
      <div class="muted st-note">Each run is a line on the map. Side = first heading vs the straight line to the first mark.</div>`;
  }

  // ---- DOM (app.js globals used from here down) --------------------------------------------
  function planLine() {
    const axes = axesOf(S);
    const nModels = (Opt.chosen || []).length;
    const ensEl = document.getElementById("optEns");
    const ens = ensEl ? parseInt(ensEl.value || "0", 10) || 0 : 0;
    const nEnsModels = (Opt.chosen || []).filter((m) => ((Opt.models || {})[m] || {}).kind === "ensemble").length;
    const n = runCount(axes, nModels, ens * nEnsModels);
    const resEl = document.getElementById("optRes");
    const cap = Math.min(MAX_RUNS, parseInt(S.cap, 10) || 24);
    if (n > cap) return { n, over: true, text: `${n} runs: over the limit of ${cap}. Take values off or raise the limit.` };
    return { n, over: false, text: `${n} runs, about ${estimateMin(n, resEl ? resEl.value : "auto")} min.` };
  }

  function paintPlan() {
    const el = document.getElementById("stPlan"), b = document.getElementById("stRun");
    if (!el) return;
    const p = planLine();
    el.textContent = p.text; el.className = p.over ? "st-plan warn" : "st-plan muted";
    if (b) b.disabled = S.running || p.over;
  }

  function set(k, v) { S[k] = v; paintPlan(); }

  function field(k, label, help) {
    return `<label class="st-field">${label} <input type="text" value="${esc(S[k])}" placeholder="none"
      oninput="Study.set('${k}', this.value)"></label>${help ? qhelp(help) : ""}`;
  }

  function mount(id) {
    const el = document.getElementById(id); if (!el) return;
    el.innerHTML = `<div class="card st">
      <h3>Study <span class="muted" style="font-weight:400">— route it many ways</span></h3>
      <div class="muted" style="font-size:12px;margin-bottom:6px">Routes the course again with one thing changed each time. Uses the race, models and settings above.
        ${qhelp("Each run changes one thing from the baseline. Models alone shows where they disagree; the blend can hide that. Leave a box empty to skip it.")}</div>
      <div class="st-axes">
        <label class="optchk"><input type="checkbox" ${S.models ? "checked" : ""} onchange="Study.set('models', this.checked)"> Each model alone</label>
        <label class="optchk"><input type="checkbox" ${S.members ? "checked" : ""} onchange="Study.set('members', this.checked)"> Each ensemble run alone</label>${qhelp("Needs Ensemble runs above 0.")}
        ${field("start_min", "Start (min)", "Minutes before (−) or after the start.")}
        ${field("polar_pct", "Boat speed %", "Sails slower or faster than the polar.")}
        ${field("tws_pct", "Wind speed %", "")}
        ${field("twd_deg", "Wind shift °", "− = shifted left, + = shifted right.")}
        <label class="st-field">Limit <input type="number" min="1" max="${MAX_RUNS}" value="${esc(S.cap)}" style="width:56px"
          oninput="Study.set('cap', this.value)"> runs</label>
      </div>
      <div class="opt-run"><button id="stRun" onclick="Study.run()">${S.running ? "Studying…" : "Run study →"}</button>
        <span id="stPlan" class="st-plan muted"></span></div>
      <div id="stProg" class="muted" style="font-size:12px"></div>
      <div id="stOut">${S.err ? `<div class="placeholder">${esc(S.err)}</div>` : resultsHtml(S.result, esc, optTz())}</div>
    </div>`;
    paintPlan();
  }

  // the baseline with every run on top, for the Gameplan map
  function asRoute(r) {
    const base = Object.assign({}, r.baseline, { race_id: r.race_id, start: r.start,
      fan_label: "Study runs" });
    const byLabel = {};
    r.rows.forEach((row) => { byLabel[row.label] = row; });
    base.candidate_paths = (base.candidate_paths || []).map((cp) =>
      Object.assign({}, cp, { color: colorOf(byLabel[cp.label] || cp) }));
    return base;
  }

  async function run() {
    if (S.running || planLine().over) return;
    const ensEl = document.getElementById("optEns"), resEl = document.getElementById("optRes");
    const chk = (id) => { const e = document.getElementById(id); return e ? e.checked : true; };
    const body = { race_id: Opt.raceId, course_id: Opt.courseId, models: Opt.chosen,
      ensemble_members: ensEl ? parseInt(ensEl.value || "0", 10) || 0 : 0,
      avoid_land: chk("optAvoid"), use_waves: chk("optWaves"), use_current: chk("optCurrent"),
      resolution: resEl ? resEl.value : "auto", axes: axesOf(S),
      cap: Math.min(MAX_RUNS, parseInt(S.cap, 10) || 24) };
    const se = optStartEpoch();
    if (se != null) body.start_epoch = se;
    const corr = typeof mcRequest === "function" ? mcRequest(Opt.raceId, Opt.chosen, optTz()) : null;
    if (corr) body.corrections = corr;                   // manual model corrections (#384), as Find route
    S.running = true; S.err = ""; jobWebVersion(true);
    const b = document.getElementById("stRun"); if (b) { b.disabled = true; b.textContent = "Studying…"; }
    const prog = (t) => { const p = document.getElementById("stProg"); if (p) p.textContent = t; };
    prog("Getting the weather…");
    try {
      const start = await (await apiPost("/api/study", body)).json();
      if (start.detail) throw new Error(start.detail);
      if (start.ok === false) throw new Error("A route or study is already running. Try again when it's done.");
      let r;
      while (true) {
        await new Promise((res) => setTimeout(res, 3000));
        const st = await (await apiGet("/api/optimize/status?job_id=" + encodeURIComponent(start.job_id || ""))).json();
        if (st.state === "done") { r = st.result; break; }
        if (st.state === "error") throw new Error(st.error || "study failed");
        if (st.state === "idle") throw new Error("The study stopped. Run it again.");
        if (st.progress && st.progress.length) prog(st.progress[st.progress.length - 1]);
      }
      S.result = r;
      if (r && r.available && r.baseline && r.baseline.available) Opt.result = asRoute(r);
    } catch (e) {
      S.err = "Study failed: " + String((e && e.message) || e);
    }
    S.running = false; jobWebVersion(false);
    renderGameplan();
  }

  return { set, mount, run, parseList, axesOf, runCount, estimateMin, colorOf, resultsHtml,
    sidesText, sourceText, fmtDelta, asRoute, MAX_RUNS };
})();

if (typeof module !== "undefined" && module.exports) module.exports = Study;
