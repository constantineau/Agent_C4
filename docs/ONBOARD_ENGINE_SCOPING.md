# Onboard Engine + Performance Lab — Scoping

**Status:** design / scoping only (2026-06-17). No code written yet. Companion to
`docs/RRS41_COMPLIANCE.md` (the *why*); this is the *how*. Supersedes the old "all-onboard needs a
local LLM (big build, deferred)" framing — see RRS41 §4.

This is a **scope extension**, provisionally a **Phase 9 / Onboard + Performance-Lab track**. It does
not change Phases 0–7; it adds a compliant in-race execution path and a between-races learning loop.

---

## 1. The three-layer architecture

| Layer | Where | In-race | What |
|---|---|---|---|
| **A. Deterministic engine** | **Onboard (Pi 4)** | ✅ legal | `navigator`, `routing`, `tactics`, `sails`, `polar_tool`, `fatigue` — physics/geometry on the boat's own sensors + published course. No LLM. Expedition-class. |
| **B. Common-data fetch** | cloud or onboard | ✅ legal | GRIB / forecast / AIS / buoys — "information available to all boats" (verbatim, no per-boat processing). |
| **C. Conversational coaching** | onboard local LLM (in-race) / cloud Opus (otherwise) | onboard ✅ / cloud ❌ | Narrate the engine's facts; free-form crew Q&A. |
| **D. Performance lab** | **Cloud Opus 4.8** | n/a (between races) | Prep, debrief, and *write-back* learning (refined polars / crossovers / calibration / fatigue). Unrestricted — not "while underway". |

Connective tissue = the **"homework" pattern**: Opus produces artifacts off-boat *before the start*,
they are **loaded onto the boat**, and the onboard system merely executes/recomputes them. The plan
**freezes at the gun**; nothing is re-derived from the cloud mid-race.

---

## 2. Layer B — relocate the deterministic engine to the Pi

The six modules already run on the VPS. The only real porting work is **data access** and
**packaging**; the algorithms are unchanged.

**Key technical wrinkle — the data source differs onboard:**
- *Cloud today:* the modules query TimescaleDB `telemetry_raw` (15-s aggregates uplinked from the
  boat).
- *Onboard:* the data is local — the Phase-2 full-res SQLite archive (`sk_archive` volume), and for
  live values the **Signal K WS directly** (full-res, lowest latency — better than the 15-s
  aggregates the cloud sees).

**9.0 — Data-access abstraction.** Put a small pluggable data layer behind the six modules:
`source = CloudTimescale | OnboardArchive(+SignalK live)`. Same module code, same outputs, different
backend. Also stage the knowledge files on the Pi (`sr33_speed_guide.md`, `polars_sr33.sql` → a local
polars store) and a slot for the **loaded race plan + refined polars**.
*Exit test:* on the bench, the engine produces outputs identical to the cloud path when fed the same
data from the local backend.

**9.1 — Onboard API + compose.** Package the engine as an onboard service in `compose.pi.yml`
(runs on the Pi alongside Signal K / uplink / archiver), exposing the same REST endpoints the iPad
already uses: `/navigator`, `/course`, `/route`, `/tactics`, `/sail`, `/polar-analysis`, `/fatigue`,
`/forecast`. No LLM, no tool-loop — direct deterministic responses.
*Exit test:* the iPad, pointed at the Pi, renders the same nav/sail/plot/tactics screens it gets from
the cloud.

**9.2 — iPad race-mode routing + cloud race gate.** In race mode the iPad talks **only to the Pi**
(channel separation at the config/network level, not a soft toggle); the cloud agent gets the
**server-side, fail-closed** RRS-41 gate (RRS41 §4A) so even if reached it refuses
tactics/routing/polar/sail/fatigue. Add the audit log (mode on/off, channel state, refusals).
*Exit test:* in race mode, no request reaches the cloud; the cloud agent refuses gated topics with the
RRS-41 message; the audit log shows it.

Latency note: onboard the engine reads full-res Signal K live data, so responses should be *faster*
than the cloud path (no uplink lag, no 15-s aggregation, no WAN round-trip).

---

## 3. Layer C — optional onboard conversational LLM (Jetson Orin Nano)

For in-race natural-language coaching. **Not required** for layers A/B; add only if narration over the
engine's facts is wanted on the water.

**Hardware:** Jetson Orin Nano **8GB, Super mode** (JetPack 6.2; ~67 TOPS, 102 GB/s, ~$249). Pi 4 stays
the sensor/engine box; the Orin is an inference companion.

**Confirmed benchmarks** (NVIDIA JetPack 6.2, INT4 / MLC, Super mode):

| Model | tok/s (Super) | Mem | Fit |
|---|---|---|---|
| **Qwen2.5-7B** | **21.8** | ~4.8 GB | ✅ primary pick — best capability + function-calling at 8GB |
| Llama-3.1-8B | 19.1 | ~4.8 GB | ✅ strong alternative |
| Phi-3.5 3.8B | 38.1 | ~2.3 GB | ✅ fast / headroom |
| Llama-3.2-3B | 43.1 | ~2 GB | ✅ fastest |
| Gemma-2-9B | 9.2 | ~5.5 GB | ⚠️ practical ceiling, slow |

Prefill ~285–300 tok/s; ~14.8 W at 25W mode for a 7B. A ~100-token tactical answer ≈ 5 s; longer
narration ≈ 15 s — usable on a boat.

**Practical caveats (must verify on the unit):**
1. **Use NVIDIA's MLC / TensorRT-LLM runtime (`jetson-containers`), not bare llama.cpp/Ollama, for
   7–8B** — a known CUDA memory-allocator regression in JetPack R36.4.7 broke >1B models under
   llama.cpp; NVIDIA's 7–8B numbers come from MLC. Pin a known-good JetPack.
2. **INT4 required** for 7–8B on 8GB — minor quality loss; fine for narration (the engine does the
   reasoning).
3. **Thermal** — Super numbers assume cooling; a hot enclosed nav box will throttle without an active
   heatsink/fan. Budget cooling.

**Design rule:** the engine computes; the local model **narrates + answers single-shot** over the
engine's structured facts — no math, no inventing tactics, only short/optional tool loops. Keeps it
accurate and fast and prevents hallucinated tactics.

**9.4 — Local LLM narrator.** Stand up the Orin with Qwen2.5-7B (A/B vs Qwen3-4B for speed), fed the
engine's facts + the crew question, single-shot.
*Exit test:* an onboard NL answer grounded in the engine's facts at usable latency, offline from any
cloud.

---

## 4. Layer D — the performance lab (cloud Opus 4.8, between races)

Unrestricted (not "while underway"). Closes the learning loop: **race → debrief → refine → load →
race**. See RRS41 §6.

**9.3 — Performance lab.**
- **Hoisted-sail logging (prerequisite).** Today the hoisted sail is only in browser `localStorage`
  (`sr33.hoisted`), passed transiently to `/sail` — **not persisted**. Add a timestamped hoisted-sail
  log to the archive so crossover-learning has labels. (Polar + calibration learning work without it.)
- **Polar refinement.** Extend `polar_tool.py` (today read-only observed-vs-ORC p90) to aggregate
  across many sails and **write back** an updated polar table (`target_stw`/`target_vmg`), replacing
  the generic ORC cert with the boat's measured polars.
- **Crossover refinement.** From hoisted-sail × achieved-speed history, refine the J1/A2/A3/S2
  crossover points → updated `sr33_speed_guide.md`.
- **Calibration learning.** Cross-source comparison across sails → speedo/wind/heel offset + drift →
  calibration factors / `source_notes`.
- **Fatigue tuning.** Tune `FATIGUE_*` against labeled real archives.
- **Prep + debrief.** Opus generates the frozen pre-race plan (forecast + refined polars) and the deep
  post-race debrief (extends the 6.2 summarizer).

*Exit test:* a real (or replayed) sail produces refined polars/crossovers via Opus, loaded back onboard
and visibly used by the engine on the next sail.

**Bright line:** all refinement is computed between races and loaded as static reference; never
re-derived from the cloud mid-race (RRS 41).

---

## 5. Phased plan (proposed)

| Step | Deliverable | Exit test | New HW |
|---|---|---|---|
| 9.0 | Data-access abstraction (cloud ↔ onboard backend) for the 6 modules | identical outputs from local data on bench | none |
| 9.1 | Onboard engine service + API in `compose.pi.yml` | iPad → Pi renders all nav/sail/plot/tactics screens | none |
| 9.2 | iPad race-mode → Pi only; server-side fail-closed cloud gate + audit log | race mode reaches no cloud; gated topics refused + logged | none |
| 9.3 | Performance lab: hoisted-sail logging, polar write-back, prep/debrief/learning loop | a sail → refined polars loaded back onboard | none |
| 9.4 *(opt)* | Orin Nano local LLM narrator | grounded onboard NL answer, offline, usable latency | Orin Nano 8GB |

9.0 → 9.2 is the compliance-critical path (legal in-race, no hardware). 9.3 is the high-value learning
loop. 9.4 is the optional NL polish that needs the Orin.

---

## 6. Open decisions / inputs needed

- **Onboard live-data source:** Signal K WS direct (full-res, recommended) vs the local SQLite archive
  vs a small onboard Postgres/Timescale. Leaning SK-live for current values + SQLite archive for
  history.
- **Race-mode channel separation mechanism:** network-level (iPad on a boat-local SSID with no WAN) vs
  app config flag. Network-level is the stronger compliance posture.
- **Orin Nano: buy now or after 9.0–9.3 prove out on real sails?** (Layers A/B deliver ~80% of value
  with no hardware; the Orin is for NL polish.)
- **Where does the engine run on the Pi 4 vs Orin?** Engine is light (deterministic) → Pi 4 is fine;
  the Orin, if added, is dedicated to the LLM.

---

## 7. Compliance summary (cross-ref RRS41_COMPLIANCE.md)

| Capability | Onboard? | In-race legal? |
|---|---|---|
| Deterministic engine on Pi (own sensors + published course) | yes | ✅ (Expedition-class) |
| Common data (GRIB/forecast) fetched verbatim | either | ✅ (available to all) |
| Local-LLM narration over engine facts | yes | ✅ |
| Cloud-LLM customized tactical/routing/coaching | no | ❌ (outside source) |
| Refined polars/crossovers computed off-boat, loaded *pre-start* | n/a | ✅ (own data, frozen) |
| Refined plan re-derived from cloud *mid-race* | n/a | ❌ |

*Confirm with the OA/RC in writing before race use; re-check the Sailing Instructions (~July 2026).*
