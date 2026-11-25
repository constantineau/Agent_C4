/* Debrief: which weather the best route used, and time lost split in two (#404).
   Server: app/oraclewind.py (report.oracle_weather) and the regret split in app/judge.py
   (regret.kind "same_wind": minutes = the plan's route vs the best route on the same wind;
   forecast_error_minutes = the plan's route on that wind vs its forecast ETA).
   Uses app.js's esc(). The Debrief tab's tactics card calls debBasisRows(report, oldRow). */
"use strict";

const DEB_ROW = '<b style="min-width:130px;display:inline-block">';

function debMinWord(m) {
  const a = Math.abs(m);
  return a + " min";
}

// "12 min slower than the best route" / "same as the best route"
function debRouteCall(min) {
  if (min == null) return "—";
  if (Math.abs(min) < 1) return "same time as the best route";
  return min > 0 ? debMinWord(min) + " slower than the best route"
                 : debMinWord(min) + " faster than the best route";
}

// real = the best route used hourly analyses (the wind that blew). Otherwise its wind is itself a
// forecast, so the gap is between two forecasts, not forecast error.
// "took 15 min longer than forecast" / "took 15 min longer on a later forecast than on its own"
function debForecastErr(min, real) {
  if (min == null) return null;
  if (real) {
    if (Math.abs(min) < 1) return "took what the forecast said";
    return "took " + debMinWord(min) + (min > 0 ? " longer" : " less") + " than forecast";
  }
  if (Math.abs(min) < 1) return "took the same on a later forecast as on its own";
  return "took " + debMinWord(min) + (min > 0 ? " longer" : " less") + " on a later forecast than on its own";
}

function debAgeTable(w) {
  const models = (w.models || []).map((m) => m.model);
  const rows = (w.hourly || []);
  if (!models.length || !rows.length) return "";
  const head = models.map((m) => `<th>${esc(String(m).toUpperCase())}</th>`).join("");
  const body = rows.map((r) => `<tr><td>${r.h} h</td>${models.map((m) => {
    const a = (r.age_h || {})[m];
    return `<td>${a == null ? "no data" : a + " h old"}</td>`;
  }).join("")}</tr>`).join("");
  return `<details style="margin-top:4px"><summary class="muted" style="font-size:12px">Age by race hour</summary>
    <table class="fleet-tbl" style="margin-top:4px"><thead><tr><th>Race hour</th>${head}</tr></thead><tbody>${body}</tbody></table></details>`;
}

function debWeatherRow(r) {
  const w = r.oracle_weather || {};
  if (!w.summary) return "";
  return `<div class="dep-row">${DEB_ROW}Weather used</b> ${esc(w.summary)}
    ${w.note ? `<div class="muted" style="font-size:12px">${esc(w.note)}</div>` : ""}${debAgeTable(w)}</div>`;
}

// The weather row plus the time-lost rows. `oldRow` is the tab's own single "Time lost" row,
// shown as is for a report from before the split (or one the split couldn't be made for).
function debBasisRows(r, oldRow) {
  const reg = r.regret || {}, pb = r.playbook || {};
  const basisNote = reg.basis && reg.basis.note ? ` <span class="muted">· ${esc(reg.basis.note)}</span>` : "";
  let lost;
  if (reg.kind === "same_wind") {
    const real = (reg.forecast_check || "real") === "real";
    const fe = debForecastErr(reg.forecast_error_minutes, real);
    const said = pb.predicted_hours != null ? pb.predicted_hours.toFixed(1) + " h" : null;
    const took = reg.plan_on_actual_hours != null ? reg.plan_on_actual_hours.toFixed(1) + " h" : null;
    lost = `<div class="dep-row">${DEB_ROW}Route choice</b> ${esc(debRouteCall(reg.minutes))}, same wind${basisNote}</div>`
      + (fe ? `<div class="dep-row">${DEB_ROW}${real ? "Forecast" : "Two forecasts"}</b> The plan's route ${esc(fe)}`
        + (said && took ? ` <span class="muted">(said ${esc(said)}, took ${esc(took)})</span>` : "") + "</div>" : "");
  } else {
    lost = (oldRow || "") + (reg.split_note ? `<div class="dep-row muted" style="font-size:12px">${esc(reg.split_note)}</div>` : "");
  }
  return debWeatherRow(r) + lost;
}
