# Onboard Copilot — iPad Crew Dashboard — Design

**Status:** design / locked (2026-06-19); **BUILT 2026-06-19/20 — phases 1–4 shipped** (static
prototype → live engine wiring + deterministic status → LLM commentary/status-refine → streamed
tap-to-detail), plus polish (wind-trend charts, forecast-vs-actual verification, demo scenarios,
day/night, feedback widget). Lives in `pi/console/dashboard/` (served at `:8091/dashboard/`); the
LLM layer is `pi/orin/copilot/dashboard_brief.py` (`POST /dashboard`) + the streamed `POST /detail`.
**Note: the literal 12-tile grid below was deliberately simplified at build time to higher-order
tiles** (commit `99c3d9d`, "crew direction"). The grid is now **8 tiles on a 4×2 layout** —
`wind, playbook, forecast, sail, eta, ais, charge, data` — with the
**VMG + Tactics tiles retired 2026-07-03** (crew de-dup): VMG (kts + % of polar) repeats the boat's own
instruments, and the on-water tactical read (favoured side / persistent-vs-oscillating) now lives in the
top **Strategy strip** — its synthesis consumes `get_tactics` and, when NO playbook is aboard, pulls it
directly so the strip still shows the favoured-side read (it no longer goes blind in practice mode). The
**AIS / Fleet** tile (onboard `GET /ais` → range/bearing/CPA/TCPA, ok/watch/act on the closing-CPA
guard; v1 is AIS proximity/collision, handicap-aware fleet tactics is the next increment) AND the
**PLAYBOOK-ADHERENCE** tile (the last "later tile" — **BUILT**, see below). Companion to
`docs/ONBOARD_ENGINE_SCOPING.md` (the three-tier architecture) and
the shipped copilot decision-support layer (`pi/orin/copilot/`).

**PLAYBOOK-ADHERENCE tile (BUILT 2026-06-23).** "Are we sailing the frozen homework, and has a
branch trigger fired?" Its truth is the **copilot**, not the engine: a deterministic
`pi/orin/copilot/adherence.py` compares the Lab-2 frozen playbook (the `recommended` start variant +
each variant's `what_flips_it` trigger, keyed by first-beat side) against the engine's live tactical
read (`get_tactics`: persistent-vs-oscillating, favored side). It returns a ready-made tile object
via **`GET /copilot/adherence`** (no LLM — always available; `na` when no playbook is aboard), which
the dashboard polls on its own ~8 s cadence (separate from the slow LLM brief). States: **ok** = on
plan (oscillating, or a persistent shift confirms the recommended side); **watch** = an oscillating
*lean* toward a non-recommended side (early warning); **act** = a persistent shift now favors a
DIFFERENT variant → the playbook's branch says switch (the tile names the variant + surfaces its
`what_flips_it`). The face shows a variant table (share %, ★ recommended, ← favored-now); the detail
carries the trigger text + grounding (`playbook:<id>`, `get_tactics`). This deterministic tile just
compares the pre-authored variants against the tactical read (onboard is legal in-race either way). Verified: pure-logic exit test
(`bench_copilot.test_adherence_logic`, all states + grounding), an end-to-end `/adherence` run
(stub engine + loaded playbook → branch fired), and a Playwright UI smoke (grid then 10 tiles; now 8 on 4×2 — VMG + Tactics retired 2026-07-03, calm
`On plan: Left` / escalated `Switch → Right`, detail + commentary, 0 console errors). *(Fixed in
passing: `narrate.py`'s playbook-branch callout read `tac["persistent"]` flat, but the engine nests
it under `tac["wind"]` — so the proactive branch callout never fired; now reads the nested path.)*

The decision-support layer already produces a grounded `DecisionBrief` (situation + factors +
recommendations, each grounded in an engine fact, with urgency + confidence). This doc specifies
how that — plus the live engine instruments — is presented to the crew on the iPad **during a
race**, at a glance, accessibly.

Locked with the user across a design conversation 2026-06-19. The build target is an evolution of
the existing **`pi/console`** race surface (already iPad-landscape, day/night via `sun.js`, talks
to the onboard engine on `:8200`); it adds the copilot service (`:8300`) as a second source.

---

## 1. The core decision — a fixed grid, not moving cards

We evaluated three layouts (fixed tile-grid, gauges + coach column, and an LLM-driven priority
stack where the important items grow/reshuffle). **Decision: a fixed grid where every item lives in
a constant position and never relocates.** A heeling, spray-soaked crew needs their eyes to land in
the same place every time — *the information changes, not the geography*.

> **The model:** all items on screen at once in a fixed grid; the **LLM scores each item's status**
> (green/yellow/red, with redundant non-color encoding); a dedicated **commentary panel** carries
> the "what matters now" prioritization in words; **tap any tile** for an LLM deep-dive. Nothing
> moves — colors, the commentary, and the open detail change.

This is deliberately the opposite of a dashboard that rearranges itself. Priority is expressed
through per-tile **status** and the **commentary panel's ordered notes**, never by motion.

---

## 2. Layout

iPad landscape. A fixed grid of ~12 status tiles (≈70% width) + a persistent copilot commentary
panel (≈30%, right column — bottom strip is an alternative, see §9). Each tile occupies one constant
cell.

**Calm:**
```
┌───────────┬──────────┬───────────┬──────────────┐
│ WIND ●ok  │ SPD  ●ok │ SAIL ●ok  │ COPILOT      │
│ 12 263°↗  │ 6.8 94%  │ J1 ✓      │ ⬤ llm-live   │
├───────────┼──────────┼───────────┤──────────────│
│ NAV  ●ok  │ LAYLN ●ok│ TACT ●ok  │ Racing clean.│
│ Cove 1.9  │ 9° below │ ◀L osc    │ Watch the    │
├───────────┼──────────┼───────────┤ left phase   │
│ FATIG ●ok │ FCAST ●ok│ ROUTE ●ok │ for a tack.  │
│ 28 fresh  │ ↗16kn    │ 2 tk      │ conf: high   │
├───────────┼──────────┼───────────┤              │
│ HEEL ●ok  │ DEPTH ●ok│ DATA ●ok  │ [ Brief me ↻]│
└───────────┴──────────┴───────────┴──────────────┘
```

**Escalated — same positions, only status + the panel changed:**
```
┌───────────┬──────────┬───────────┬──────────────┐
│ WIND ●ok  │ SPD ▲wtch│ SAIL ■ACT │ COPILOT      │
│ 12 263°↗  │ 6.8 88%  │ J1→A3 peel│ ⬤ llm-live   │
├───────────┼──────────┼───────────┤──────────────│
│ NAV  ●ok  │ LAYLN ●ok│ TACT ▲wtch│ ▸ PEEL J1→A3 │
│ Cove 1.9  │ 9° below │ ◀L osc    │   pre bear-  │
├───────────┼──────────┼───────────┤   away (SAIL)│
│ FATIG ■ACT│ FCAST ●ok│ ROUTE ●ok │ ▸ Helm idx 72│
│ 72 rotate │ ↗16kn    │ 2 tk      │   rotate(FAT)│
├───────────┼──────────┼───────────┤ conf: med    │
│ HEEL ●ok  │ DEPTH ●ok│ DATA ●ok  │ [ Brief me ↻]│
└───────────┴──────────┴───────────┴──────────────┘
```

**Tiles (lock the set; greyed "coming soon" until their data exists):** WIND, SPEED (+ polar %),
SAIL, NAV, LAYLINE, TACTICS, FATIGUE, FORECAST, ROUTE, DATA-HEALTH, HEEL/TRIM, DEPTH. Later:
AIS/FLEET, PLAYBOOK-ADHERENCE (Lab-2). *(As built: an **8-tile 4×2** higher-order set —
wind, playbook, forecast, sail, eta, ais, charge, data — incl. **AIS/FLEET** and **PLAYBOOK** (the
adherence tile, since unified onto the engine's `/selector`); VMG + Tactics were retired 2026-07-03
(VMG repeats the boat's instruments; the tactical read lives in the Strategy strip). See the status
note at the top.)*

---

## 3. Two layers, two data rates (and why the LLM can feed it)

The LLM **can and should** feed the dashboard — but not the live numbers, because of latency
(~45 s warm, ~2 min cold). Three data rates, and keeping them separate is the whole design:

| Layer | Source | Refresh | Drives |
|---|---|---|---|
| **Instruments** | engine `/conditions` | 1–2 s | the live numbers on each tile |
| **Engine-derived** | `/navigator` `/sail` `/tactics` `/fatigue` `/route` | 5–15 s | tile values + a threshold status |
| **Synthesis** | copilot `/brief` | on-demand / 1–5 min | per-tile **LLM status refinement** + the commentary |

> **The engine owns the dashboard's truth** (every tile value + a deterministic status works with
> the LLM off). **The copilot refines the status and writes the commentary.** The dashboard is never
> blank, never waiting on the model.

**Refresh model (locked): instant deterministic + LLM catch-up.** Tiles render with their
deterministic threshold status the moment engine data lands; the copilot brief runs in parallel and
*upgrades* the statuses + commentary when ready (~45 s), with a quiet shimmer and a soft transition.
LLM down/slow/timeout → the dashboard stays on the deterministic statuses; the mode pill reads
"engine read" instead of "llm-live."

**Grounding-as-routing.** Every brief item already carries `grounded_in: ["get_sail_advice"]` — that
tag *is* the address of the tile it belongs to. One brief object drives the whole overlay: a
recommendation grounded in `get_sail_advice` → escalates the SAIL tile + appears in the commentary
tagged "(SAIL)". No per-domain wiring.

---

## 4. How the LLM scores a tile (grounded, flicker-free)

- **Deterministic status instantly** — the engine knows the thresholds, so each tile is green/yellow/
  red immediately.
- **LLM refines it** — its value is *context over thresholds*: "88% of polar but normal in this chop
  → keep yellow not red," or "94% but pointing too low → flag it." It may up/downgrade a tile's
  status **only when grounded in a fact**, and explains the call in the commentary / detail. The
  grounding guardrail from the decision-support layer still holds: a status rests on a fact or it
  isn't shown.
- **Hysteresis + dwell (no flicker — a hard requirement).** Status changes use a Schmitt-trigger: a
  tile must clear a *promote* band to worsen and fall below a lower *demote* band to recover, with a
  ~30–45 s minimum dwell before any status change. Colors settle at a human pace; they never strobe.
  Same DNA as the Phase-6 alert debounce ("raise slow, clear fast") and the fatigue baseline.

---

## 5. Status encoding — accessible by construction, always on

**Decision (user, 2026-06-19): visual accessibility is ALWAYS ON — there are NO accessibility option
toggles.** The dashboard has exactly **two themes — daytime and night** (the existing `sun.js`
AUTO/DAY/NIGHT, where AUTO switches on local sunrise/sunset). Both are accessible *by construction*;
there is no separate "color-blind mode" to find or forget. The crew gets a fully accessible
instrument in either theme, period.

This is enforced by never letting color carry status alone. ~8% of men have red-green color vision
deficiency and a race crew skews male, so **status is always encoded in ≥3 independent channels** so
any one can be lost (color blindness, night-mode red tint, glare):

1. **Shape / icon (strongest non-color channel):** OK = ● filled circle · WATCH = ▲ triangle ·
   ACT = ■ filled square (or ⬢ octagon = "stop"). Distinct silhouettes, road-sign style, legible in
   pure grayscale.
2. **Word / letter label:** a small `OK / WATCH / ACT` chip — color-independent, unambiguous.
3. **Luminance + a left severity bar:** the three statuses differ in *brightness* (not only hue),
   and a left edge-bar thickens as severity rises → severity legible by edge alone.

**The palette is color-blind-safe for everyone** (no standard-vs-CVD choice). The hue layer rides on
top of the three channels above using a blue / amber / vermillion ramp (Okabe-Ito style): blue = ok,
amber = watch, red-orange = act — blue-vs-orange is distinguishable across virtually all CVD types and
red-orange still reads as "alert." Tiles may additionally fill from the bottom like a gauge so
severity is also a *position* (legible even in grayscale).

**The two themes share one accessible system; hue is hue-optional by design.** Because shape + label +
luminance + severity bar already carry the status, hue is only a bonus channel — which is exactly what
makes the **night theme** work: `sun.js` night is red-on-black, so **red can't mean "act"** at night,
but it doesn't need to (the icon, label, brightness, and bar carry it; night uses amber/white accents
instead of red). One encoding, two themes, accessible in both — no toggle. ACT may additionally use a
gentle **pulse-in-place** (a fixed tile pulsing is accessible motion; relocating cards is not — the
distinction the crew cares about).

---

## 6. The commentary panel

A persistent panel (right column or bottom strip): a one-line **focus headline**, 2–3 ordered
**notes** each tagged with the tile it references (SAIL, FAT…), overall **confidence**, the **mode
pill** (llm-live / engine read), and a **Brief me ↻** button to force a fresh synthesis. Tapping a
note flashes a **highlight ring** on its tile — the LLM "points" without anything moving. This is
where prioritization lives, so the grid never reshuffles.

---

## 7. Tap a tile → LLM deep-dive

Tap any tile → a **slide-over** (the grid stays glanceable behind it). Three layers arrive in order:

1. **Instant (0 ms):** the full engine numbers for that domain + the tile's `note` from the last
   brief (already downloaded) — never blank.
2. **Graphical detail (instant):** reuse what exists — SAIL → the sail dial; NAV/LAYLINE → the
   course plot; FATIGUE → its component bars; WIND/FORECAST → a trend sparkline.
3. **LLM deep-dive (streams in):** a **scoped** copilot query ("explain this tile") with a tool
   subset (the tile's tool + a neighbor or two) and a short answer, **streamed** token-by-token
   (`stream:true`). Streaming is the key latency lever — words start in ~1 s warm instead of a 45 s
   blank. A scoped query is much cheaper than the full 10-domain brief.

```
┌──────────────────────────────────────────────┐
│ ‹ back        FATIGUE   ■ ACT   conf: med      │
├──────────────────────────────────────────────┤
│  index 72  ▮▮▮▮▮▮▮▯▯▯  rotate_soon             │
│  heading ▮▮▮▮  reversals ▮▮▮▮▮  spd-def ▮▮▮     │
├──────────────────────────────────────────────┤
│ WHY  Heading instability and steering          │
│ reversals are both above the driver's 40-min   │
│ baseline; speed deficit is creeping ▌…         │  ← streaming
├──────────────────────────────────────────────┤
│ CONSIDER  Plan a rotation in the next few min. │
│ CLEARS WHEN  index < 60 sustained.             │
│ BASED ON  get_fatigue  ⌄ (tap → raw facts)     │
├──────────────────────────────────────────────┤
│ Ask about this…                          [ ↵ ] │
└──────────────────────────────────────────────┘
```

**Detail template (same five slots on every tile, learned once):**
- **WHY** — the LLM's grounded read of this status (streamed).
- **CONSIDER** — the recommendation(s) for the domain, with urgency + confidence.
- **CLEARS / TRIGGERS WHEN** — the threshold that flips the status (makes it legible, not magic).
- **BASED ON** — the grounding, tap-to-expand to the **raw facts** (TWS 16, TWA 130, optimal S2…) — a
  trust feature serving the never-sole-authority rule.
- **Ask about this** — a scoped follow-up.

**The follow-up is how conversational chat comes onboard safely.** The console deliberately dropped
the open LLM chat; a **domain-scoped, grounded** follow-up runs through the copilot's bounded tool
loop (limited to that domain's tools, every answer grounded) — conversational depth without an
open-ended chatbot that could wander off the facts or out of compliance.

**Latency tactics:** the dashboard's periodic brief keeps the model **warm**, so taps land on a
loaded model; for the always-hot **ACT** tiles, **prefetch** the detail in the background so red
tiles open instantly. Slide-over (grid stays visible), **swipe** tile-to-tile, **auto-timeout** back
to the grid after ~30–60 s idle, big back target.

---

## 8. The data contract (small, reuses what's built)

- **Brief grows a per-tile map** (deterministic fills it from thresholds instantly; the LLM refines
  `status`/`note`):
  ```json
  { "tiles": { "sail":   {"status":"act",   "value":"J1→A3 peel", "note":"…",
                          "clears_when":"TWA<100°", "grounded_in":["get_sail_advice"], "confidence":"med"},
               "speed":  {"status":"watch", "value":"6.8 88%", "note":"…",
                          "grounded_in":["get_conditions"], "confidence":"high"} },
    "situation": "…", "recommendations": [ … ], "confidence":"med" }
  ```
- **`POST /detail {domain, question?}`** — runs the scoped copilot loop with a domain tool-subset and
  **streams** the explanation, returning the `consider` / `clears_when` / `based_on` structure. It's
  `make_brief` with a narrower tool set + a focus prompt — assembly, not new machinery.
- **Streaming added to `llm.py`** (`stream:true`) for the detail path.
- The dashboard fires `make_brief(use_llm=False)` instantly + `use_llm=True` in parallel (or one
  endpoint that returns deterministic then a follow-up).

---

## 9. Compliance (RRS 41)

Unchanged from the three-tier design: this all runs **onboard** (Pi engine + Orin LLM), over
boat-local Wi-Fi, on the boat's own sensors + common public data, never phoning the cloud mid-race.
The LLM interprets engine facts and matches conditions against the frozen playbook; it never
originates strategy (descope 2026-07-06 — docs/PLAYBOOK_V2.md §7) and never does the math (the
engine does — a reliability guardrail, not the RRS-41 line). Legal in-race because it's all onboard.
See `docs/RRS41_COMPLIANCE.md`.

---

## 10. What's buildable now vs. needs adding

- **Now:** the fixed grid (engine endpoints already feed values + thresholds), per-tile LLM status +
  commentary via grounding-as-routing, the instant+catch-up refresh, the accessible status encoding,
  tap-to-detail with instant facts + reuse of the existing dial/plot.
- **Small adds:** the `tiles` map in the brief; `POST /detail` + `llm.py` streaming; the front-end
  ranking/hysteresis + theme toggle (pure JS in `pi/console`); the onboard **polar-% tool** (the
  SPEED tile's "94%").
- **Done since:** voice (the narration increment); the **AIS/FLEET tile** (onboard `GET /ais` via a
  source-agnostic `ais.py` + other-vessel Signal K capture in `OnboardSource`); the
  **PLAYBOOK-ADHERENCE tile** (deterministic `copilot/adherence.py` + `GET /copilot/adherence`,
  on-plan/branch-fired against the frozen Lab-2 variants — see the status note up top).
- **Later:** handicap-aware **fleet** tactics on the AIS tile (roster → corrected-time delta, needs
  the RaceDefinition `fleet` block onboard); proactive auto-coach timer (toward proactive callouts).
- **Needs real sailing data:** TACTICS, FATIGUE, ROUTE read empty on the Baltic sample bench; they
  come alive on real boat data.

**Suggested phasing:** (1) static clickable prototype in `pi/console` (fake data — eyeball the look,
incl. CVD/night toggles); (2) wire the grid to live engine instruments + deterministic statuses; (3)
add the brief overlay (LLM status refine + commentary); (4) tap-to-detail with streaming; (5)
polar-% + the later tiles.

---

## 11. Open decisions (not yet settled)

*(Resolved: accessibility is always on, no toggle; one color-blind-safe palette for everyone; two
themes only — daytime + night — both accessible by construction. See §5.)*

1. **Status count** — 3 (ok/watch/act), or a 4th gray "info/stale" state for missing/stale data
   distinct from "act"?
2. **Commentary placement** — right column or full-width bottom strip (wider/bigger tiles)?
3. **Detail follow-up scope** — keep "Ask about this" strictly in-domain, or let it widen when the
   question clearly spans domains?
4. **Prefetch policy** — ACT tiles only, top-N by priority, or lazy on tap?
5. **"Clears/triggers when"** — always shown (teaches the system), or only on yellow/red tiles?
6. **Grid contents** — lock all ~12 tiles now with greyed "coming soon," or only show tiles whose
   data exists today?
