# RRS 41 / Bayview Mackinac NOR — Compliance Review

**Status:** review complete 2026-06-17 against the **2026 Bayview Mackinac Race Notice of Race**
(`2026NOR V6 111925 Approved_Post`, approved by MRA/BYC Board 2025-11-19) and the **Racing Rules of
Sailing 2025–2028**. This is an engineering compliance read, **not a rules ruling** — confirm the
interpretation in writing with the Organizing Authority / Race Committee before relying on it, and
re-check the Sailing Instructions (published ~July 1, 2026), which can change the NOR and take
precedence (NOR §2.1(c)).

---

## 1. The governing text

**RRS 41 (Outside Help)** — a boat shall not receive help from any outside source, except:
- (a) help for a crew member who is ill, injured or in danger;
- (b) after a collision, help from the crew of the other vessel to get clear;
- (c) help in the form of information freely available to all boats;
- (d) unsolicited information from a disinterested source, which may be another boat in the same race.

**2026 NOR §2.1(d) — changes RRS 41(c)** (verbatim):

> "Help in the form of information available to all boats is permitted even if that information is
> only accessible at a cost; **however, such 'at cost' help shall not include private forecast or
> tactical advice or information customized for a particular boat or group of boats while
> underway.** This changes RRS 41(c)."

Other relevant NOR points: the race is governed by RRS 2025–2028 + the NOR + SIs (§2.1); ORC rating
applies (§2.1(i)); World Sailing **Appendix WP** (Racing Around Waypoints) applies (§2.1(m)); the
**Finish** and the **Cove Island Gate** are defined by the boat's **position transponder** plus a
**photo of the primary navigation GPS** at crossing (§2.1(f), §8). The NOR carries **no blanket ban
on carrying or using onboard electronics/communications** — the operative constraint is RRS 41 as
narrowed by §2.1(d): the issue is the *kind of help received from an outside source while underway*,
not the presence of gear.

---

## 2. What this means for the SR33 AI Navigator

The deciding question under RRS 41 is **"is the boat receiving help from an outside source while
racing?"** — and §2.1(d) makes explicit that **private/customized forecast or tactical advice
"while underway" is not permitted**, even if you pay for it. The line is *customized-for-this-boat
from outside* vs. *the boat's own equipment* vs. *info equally available to everyone*.

Mapping our features onto that line:

| Feature | In-race status (cloud agent) | Why |
|---|---|---|
| Passive telemetry collection + archive | ✅ Allowed | No help is *received*; pure logging. |
| Live instrument readout / `/conditions` strip | ✅ Allowed | The boat's **own** instruments — not an outside source. |
| AIS collision guard, depth / stale-data **safety** alerts | ✅ Defensible | Safety (RRS 41(a) spirit); own AIS + onboard-style CPA. Keep it strictly safety, not tactics. |
| Public forecast **verbatim** (e.g. a GRIB/NWS product available to all) | ✅ Allowed | "Information available to all boats" (§2.1(d) first clause). |
| **Tactics** (favored side, shifts, leverage) | ❌ **Prohibited in-race** | Tactical advice customized for this boat from a shore source while underway. |
| **Weather routing / isochrone optimal route** | ❌ **Prohibited in-race** | Customized routing/forecast advice for this boat while underway. |
| **Polar coaching, sail crossover/peel calls, fatigue rotation, "% of polar"** | ❌ **Prohibited in-race** | Performance/tactical advice customized for this boat from outside. |
| Navigator basics (mark bearing/distance/ETA, laylines) | ⚠️ Gray | Plain navigation off the boat's own GPS is normally fine; but delivered *from a shore source* it is still "outside." Treat as gated; safest to compute onboard. |
| Debriefs / summaries **after** racing, practice, deliveries | ✅ Allowed | Not "while racing." This is the system's unrestricted use. |

**Bottom line:** with the **current cloud architecture** (Pi → Starlink → shore VPS → Claude API →
crew), every *performance/tactical/routing/coaching* answer is **customized advice generated off the
boat and delivered while underway** — squarely what RRS 41 + NOR §2.1(d) prohibit during a race.
Safety, own-instrument readout, verbatim all-boats info, and all non-racing use remain fine.

---

## 3. Considered and rejected: the "make it public" loopholes

A natural idea is to dissolve the "customized for a particular boat" problem by making the *service*
or its *outputs* public. Two versions were evaluated; **both fail**, and the rule text forecloses
them almost word-for-word.

**Version A — a public multi-tenant service.** "Anyone can sign up, upload their own polars, and use
the same system; the channels (TWS/AWA/GPS…) are universal — so isn't it 'information available to
all boats'?"

**Version B — a public tactical feed.** "Publish every boat's AI tactical advice with no password,
so all competitors can see all the advice — now the information really is available to all boats."

### Why both fail

1. **The decisive hook — "or group of boats."** §2.1(d) excludes advice "customized for a particular
   boat **or group of boats** while underway." The drafters explicitly anticipated the "make it for
   everyone / a group" move and wrote it out. So:
   - *"It's still per-boat advice, just published"* → customized for **a particular boat**. Caught.
   - *"It's one public tactical feed for the whole fleet / all our users"* → customized for **a group
     of boats** while underway. Caught.
   There is no third framing — boat-specific or group-specific, while underway, from an outside
   source, both are named exclusions.

2. **"Customized" is about how the advice is computed, not who can read it.** "Boat X at position P
   should tack now" is customized whether one person or ten thousand can see it. Publicity defeats the
   word *private* (prong 1), but not *customized* (prong 2) — and prong 2 is independent.

3. **RRS 41's root prohibition is untouched by publicity.** "A boat shall not receive help from any
   outside source." A boat that acts on the shore agent's bespoke call **received outside help** —
   that a competitor could also read the instruction is irrelevant to whether *this* boat received
   it. The protest question is "did this boat receive outside help that improved its position?", not
   "was the help secret?"

4. **"Available to all boats" means *common information*, not a public bucket of individualized
   advice.** The exception is meant for one product identical for everyone — a GRIB, an NWS forecast,
   a race-committee weather broadcast. A public wall of 200 boats' individual instructions is 200
   customized advices sharing a URL; each boat still consumes the one made for it.

5. **Self-defeating anyway.** If the feed were truly equal and public it confers no competitive edge
   (rivals see your plan too) — yet you'd still be acting on an outside source's bespoke call, which
   is the violation. RRS 41 is "no outside help," not "no advantage."

### Version C — the "public-utility LLM"

A third framing (raised 2026-06-17): the **Claude API itself is a service available to all boats** —
anyone can pay for it, like the "at cost" GRIB the NOR expressly permits — so isn't an answer it
returns "information available to all boats," especially if the agent "lives" onboard the Pi and
merely *calls out* to the model? **It fails, for reasons distinct from A/B:**

6. **"Available to all" is about the *product*, not the *provider*.** The exception saves a common
   product identical for everyone (a GRIB, an NWS forecast, an RC broadcast). A weather *service* is
   also available to all — yet a **customized routing call** from it while racing is the textbook RRS
   41 violation. The provider being open does not launder a bespoke output. When the Pi sends "boat
   C4 at P, TWS 12, TWA 45 — tack?", the *answer is customized for this boat and this moment*; the
   model's universal availability is irrelevant.
7. **The "customized for a particular boat" prong is independent and unbeatable here.** Even granting
   the model is "available to all" (defeating *private*), any boat-specific answer is still "tactical
   advice customized for a particular boat while underway" — separately excluded by §2.1(d). You must
   clear *both* prongs; customization cannot be cleared for a per-boat answer. (Same structure as the
   "or group of boats" kill shot above, aimed at a different loophole.)
8. **Orchestrator location is cosmetic.** Compliance turns on *where the customized reasoning is
   computed*, not where the calling code runs. A satphone "lives on the boat" too; calling a shore
   router with it is still outside help. A Pi that ships your specifics to Claude and receives a
   customized answer **received outside help over the link** — the advice originated off-boat.

The legitimate residue: the cloud LLM may serve as a **generic reference** (common sailing knowledge,
no boat specifics) or a **dumb pipe for common data** (verbatim public GRIB/forecast). The instant
your position/wind/boat enters the prompt and a customized answer comes back, you are over the line —
which is precisely why boat-specific in-race NL coaching must run on an **onboard** model (§4C).

### The legitimate public lane (where the instinct *does* land)

There **is** a compliant in-race public lane, upstream of the tactical call: **conditions-level,
non-boat-specific information that is genuinely the same for everyone** — a public wind / pressure /
shift observation feed for the race area, general forecasts, a shared buoy/observation layer. "The
left side has more pressure" is arguably common racecourse information; **"you, boat X, go left now"
is not.** Even here, keep it to objective *data* (wind obs, forecasts); *AI tactical opinion* about
the fleet edges back toward "tactical advice." A public service can broadcast common race-area
**data** to all in-race; the instant it computes *your boat's* move, public or not, it is back over
the line.

### Bottom line

Publishing the outputs does not cure the in-race problem. **This is ultimately a rules
interpretation, not an engineering choice** — if the public-feed theory is to be pursued, put the
exact proposal to the OA/RC in writing and get a ruling; do not rely on this reading. The risk is
asymmetric (a wrong call on a 200+ nm race is a DSQ, not a tactical regret), so default conservative.
The compliant paths in §4 are unchanged.

---

## 4. The compliant architecture — separate *computation* from *the LLM*

The earlier framing (cloud agent = illegal; all-onboard local model = legal-but-huge) conflated two
different things. Compliance turns on **where the customized reasoning is computed**, not whether an
LLM is involved:

- **The deterministic engine** — `navigator.py`, `routing.py`, `tactics.py`, `sails.py`,
  `polar_tool.py`, `fatigue.py` — is plain physics/geometry on the boat's own sensors + the published
  course. It is *not* an LLM. **This is exactly what Expedition is**, and Expedition is legal in-race
  because it is the boat's own computer crunching the boat's own data. Nothing about it is
  intrinsically cloud — it runs on the VPS today only by accident of where the stack was stood up.
  **Run it on the Pi → it is the boat's own gear → legal in-race.**
- **The conversational LLM** — the Claude call that turns numbers into language and answers free-form
  questions. *This* is the genuinely off-boat part RRS 41 bites. Most in-race value (laylines,
  routing, sail crossovers, tactics math, polar targets, fatigue) does not need it.

So the corrected picture is **three layers**:

| Layer | Runs where | In-race | Role |
|---|---|---|---|
| Deterministic engine (routing/polars/tactics/sails/nav/fatigue) | **Onboard (Pi 4)** | ✅ legal | Expedition-class; boat's own gear + published marks |
| Common-data fetch (GRIB/forecast/AIS/buoys) | cloud or onboard | ✅ legal | "Information available to all boats" — even at cost |
| Conversational coaching / NL Q&A / **in-race strategy** | **onboard local LLM** in-race; **cloud Opus** otherwise | onboard ✅ / cloud ❌ | Interpret the engine's facts, deep Q&A, **and originate strategy** over own-data + common data |

### The line is on-boat vs off-boat — NOT LLM-vs-engine, NOT interpret-vs-originate

> **Scope note (2026-07-06):** the legal analysis in this section stands — onboard origination IS
> permitted under RRS 41 — but as a **product choice** the copilot does not use that latitude: it
> narrates the engine's reads and matches conditions against the pre-authored playbook only
> (`docs/PLAYBOOK_V2.md` §7). "May originate" below describes what the rule allows, not what the
> system does.

A model running on hardware **physically on the boat**, on the crew's own gear, over the boat's own
data + genuinely-common public data, is **not an outside source** — it is onboard equipment, the same
category as Expedition, B&G, or a human tactician. All of those **originate customized tactical/routing
strategy in-race**, every day, under RRS 41, with no issue. So the onboard LLM **may originate strategy
in-race** for the identical reason the deterministic engine may compute a customized route: both sit on
the *legal* side of the only line that matters.

The thing that ever makes tactical help illegal here is that it **arrives from off the boat** — a shore
router, a coach boat, or a cloud service reached over the internet mid-race. That off-boat round-trip —
NOT "it's an LLM," and NOT "it originated the idea" — is the violation (§3, Version C). Two lines were
being conflated: the real one is **on-boat vs off-boat**; a second, self-imposed one (the onboard LLM
"only narrates / never originates strategy") is **not required by Rule 41**. Keep the remaining copilot
guardrails — *the engine does the math* (the small model has no calculator) and *every claim is grounded
in engine facts / the frozen playbook / common data* — but keep them for the right reason: they are
**correctness/reliability discipline for a 7B, not compliance requirements.** The pre-race Opus playbook
likewise stays a **strong prior** (frontier model + full GRIB beats the onboard 7B), but it is a prior,
**not a legal cage** — onboard, the copilot may depart from it and say so.

**Operating modes that fall out:**

**A — Cloud race gate (minimum-now).** Until the onboard engine exists, the current cloud-only build
must enforce a **server-side, fail-closed** race-mode gate: in race mode the cloud agent answers only
safety + own-instrument readout + all-boats info verbatim, and *refuses* tactics/routing/polar/sail/
fatigue with an explicit "racing — outside tactical help withheld (RRS 41)" reply. The Phase-5
Race/Practice toggle gates only the UI today; this moves enforcement server-side so compliance does
not depend on the crew avoiding a button. Build this regardless — smallest change that makes the
present build safe to take racing.

**B — Onboard engine (the real unlock).** Relocate the six deterministic modules to the Pi 4 with an
onboard API; in race mode the iPad talks to the Pi, not the cloud. Legal **today**, needs **no LLM at
all** — ~80% of the racing value. See `docs/ONBOARD_ENGINE_SCOPING.md`.

**C — Onboard conversational coaching (optional, hardware).** Add a local LLM on a **Jetson Orin Nano
(8GB, Super mode)** for in-race natural-language coaching over the engine's facts. Confirmed feasible:
**Qwen2.5-7B INT4 via MLC ≈ 21.8 tok/s, ~4.8 GB** (Llama-3.1-8B ≈ 19; Llama-3.2-3B ≈ 43 for headroom;
NVIDIA JetPack 6.2 benchmarks). Design rule (a *reliability* rule, not a legal one): the engine
computes the numbers; the local model interprets them, answers free-form, and **may originate strategy**
— constrained only by grounding (reason from engine facts / the frozen playbook / common data, don't
fabricate) and short tool loops, because that keeps a 7B trustworthy. All onboard → legal in-race
whether it narrates or strategizes.

The earlier "all-onboard needs a local model (deferred, big)" reading was too pessimistic: a local LLM
is only layer C; layers A and B require no new model and no new hardware.

---

## 5. Implementation patterns for the gate

- **Pre-loaded "homework."** Before the start, the cloud (Opus) produces the customized work — route,
  sail plan, polar targets, contingencies — and it is *loaded onto the onboard system*. In-race the
  onboard system executes, recomputes, AND may **re-strategize** off the boat's own sensors + common
  public data. Consulting a plan made before racing is your own preparation (like a tide table or
  pre-marked chart), not in-race outside help. **Bright line: the CLOUD plan freezes at the gun.**
  Re-running the *cloud* mid-race to update it with a fresh forecast is new outside help and prohibited —
  but the *onboard* copilot re-working the plan on its own hardware is the boat's own computation, and is
  fine. The freeze is on the off-boat link, not on onboard thinking.
- **Physical channel separation, not a software flag.** In race mode the iPad reaches *only* the Pi;
  the cloud route is disabled at the network/config level. Provable and fails closed — "the cloud was
  unreachable during the race" beats "we promise we didn't ask it anything."
- **Fail-closed, auto-engaging race mode.** Default to *restricted*; auto-engage from a
  race-window/geofence (the boat already must carry the position transponder per NOR §8 — tie to it).
  If mode is uncertain, withhold.
- **Audit trail.** Log race-mode on/off (timestamped, geofenced), the cloud channel state, and the
  request/refusal record — a tamper-evident artifact for a protest committee.

---

## 6. The C4 Performance Lab — frontier models between races (fully unrestricted)

Everything *not while underway* — preparation, debrief, and learning — is RRS-41-unrestricted, and is
where frontier **Opus 4.8** belongs. The cloud becomes the **shore-based analyst/designer** that
improves the onboard system between races (the "homework" producer), closing a learning loop:

1. **On the water:** onboard engine + local LLM execute on the *currently loaded*
   polars/crossovers/calibration; the Phase-2 full-res archive captures everything.
2. **Post-race (Opus):** backfill the log → cloud → deep debrief + **write-back refinements**.
3. **Pre-next-race (Opus):** generate the frozen race plan from forecast + the refined polars.
4. **Load onboard** before the start. Repeat — the boat gets faster each race while the in-race system
   stays clean (it only runs pre-loaded, onboard-computed data).

Frontier-model write-back capabilities (all between-races):
- **Polar refinement** — extend Phase 6.3 `polar_tool.py` (today read-only observed-vs-ORC) to
  aggregate across many sails and *emit an updated polar table* (`target_stw`/`target_vmg`) replacing
  the generic ORC cert. Using your own measured polars in-race is fine — own performance reference,
  frozen pre-start.
- **Crossover refinement** — refine the J1/A3/S1 sail-change points from observed performance →
  updated `sr33_speed_guide.md`. **Prerequisite (data gap):** the hoisted sail is currently only in
  browser `localStorage` (`sr33.hoisted`) and passed transiently to `/sail`; it is **not persisted to
  the archive**. Crossover-learning needs a timestamped hoisted-sail log — add that capture. (Polar +
  calibration learning work without it.)
- **Calibration learning** — detect speedo/wind/heel offset + drift by comparing redundant sources
  across sails → calibration factors / `source_notes`.
- **Fatigue tuning** — tune the first-cut `FATIGUE_*` thresholds against labeled real archives.

**Route-strategy studio + onboard playbook (see `docs/ONBOARD_ENGINE_SCOPING.md` §4).** The Lab also
compiles, *pre-race*, a **playbook** of N pre-optimized routing variants + a deterministic decision
tree; in-race the **onboard** executor selects, recomputes, and — when the situation outruns the
pre-authored branches — **re-strategizes** among/beyond them. (The playbook is a strong *prior*, not a
cage: pre-race Opus + full GRIB is smarter than the onboard 7B, so departing from it should be
deliberate — but it is legal, because it's the boat's own onboard computation.) Compliance hinges on
three points:
- **Public GRIB *and buoy* obs are "information available to all boats"** (§2.1(d) first clause — even
  at cost). Fetching NOAA NDBC / GLOS buoy obs + GRIB **in-race** and processing them **onboard** is
  legal — it is common data, identical for everyone, not advice computed off-boat. (Same lane as a
  shore weather broadcast.)
- **The in-race optimizer/selector/strategist runs onboard.** Picking among pre-loaded variants by fixed
  rules, re-running the isochrone optimizer onboard on the latest public GRIB, OR the onboard LLM
  synthesizing higher-order signals (forecast-vs-actual, fleet positioning, route deviation) into a fresh
  plan suggestion — all are the boat's own computation (Expedition core + an onboard tactician). Never
  phone the cloud mid-race for a fresh customized route.
- **Glass-box rationale is compliance-clean** because the *why/tradeoffs* are **authored by Opus before
  the start** (baked into the playbook) and merely **surfaced + narrated onboard** in-race — not a fresh
  cloud call. Showing the crew the tradeoffs strengthens, not weakens, the RRS-41 posture: the boat
  presents its own pre-made plan + public data, and the crew decides.

**Bright line repeated:** all refinement + strategy is computed *before the start* (or onboard in-race
on own-data + common public data) and loaded as static reference; never re-derived from the cloud
mid-race.

---

## 7. Action items

1. **Before any race use, confirm with the OA/RC in writing.** Put precise, easy-to-say-yes-to
   questions: (a) an onboard computer with no off-boat link running deterministic routing/polars on
   the boat's own sensors + the published course (this is Expedition — near-certain yes); (b) the same
   pulling public GRIB/forecast in-race but computing the route *onboard*; (c) a plan generated
   off-boat *before the start* and merely consulted during the race (the homework model). Re-check the
   **Sailing Instructions** when published — they can change the NOR and take precedence.
2. **Default to safe:** in a race with the cloud agent, restrict to passive collection + safety +
   own-data; full coaching only for **practice, deliveries, and debriefs**.
3. **Build the server-side, fail-closed race gate (§4A)** — the minimum-now change; refuse
   tactics/routing/polar/sail/fatigue in race mode with an explicit RRS-41 message, and log it.
4. **Scope/build the onboard deterministic engine (§4B)** — relocate the six modules to the Pi with an
   onboard API; iPad → Pi in race mode. Legal in-race, no LLM. See `docs/ONBOARD_ENGINE_SCOPING.md`.
5. **Optional layer C:** Jetson Orin Nano (8GB) + Qwen2.5-7B for in-race conversational coaching
   (confirmed ~21.8 tok/s INT4/MLC). Single-shot narration over the engine's facts.
6. **Stand up the C4 Performance Lab (§6):** add timestamped **hoisted-sail logging** to the archive
   (prerequisite for crossover learning), extend `polar_tool.py` to *write back* refined polars, and
   wire the prep/debrief/learning loop on cloud Opus.
7. Carriage of the **position transponder** and **primary-nav-GPS finish/gate photos** is required by
   the NOR (§2.1(f), §8) — orthogonal to RRS 41, but the boat must carry/operate them.

---

*Sources: World Sailing RRS 2025–2028 Rule 41 (racingrulesofsailing.org); 2026 Bayview Mackinac
Race Notice of Race, `2026NOR V6 111925 Approved_Post` (bycmack.com). Re-verify against the
as-published Sailing Instructions before the race.*
