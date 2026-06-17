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

## 4. Two compliant operating modes

**A. Cloud agent in a race → "compliant race mode" (restrict the advice channel).**
During racing, the shore agent must deliver only: (1) safety/collision information, (2) the boat's
own instrument data, and (3) information equally available to all boats *verbatim* (no customized
routing/forecast). It must **decline** tactics, routing, polar/sail coaching, and fatigue calls
while racing. The Phase-5 **Race/Practice toggle** is the natural control, but today it only gates
those panels in the **UI** — the chat/LLM can still answer a tactical question. **Recommended
follow-up:** enforce the gate **server-side** (in race mode the agent refuses customized
tactical/forecast/routing/coaching and answers only safety + own-data + all-boats-info), so
compliance doesn't depend on the crew avoiding a button. This is a concrete, buildable change.

**B. All-onboard for full in-race coaching (no shore loop).**
Run the agent **on the boat** (on the Pi) using a **local model with no external API call**, fed
only by the boat's own sensors. The boat's own equipment and onboard computation are **not an
"outside source"** — this is the same category as widely-used onboard nav/routing software
(Expedition, etc.). This is the only way to keep the full tactical/routing/coaching value **legally
in a race**. Note: an "onboard" agent that still calls the **cloud Claude API** is **not** clearly
compliant — the customized advice would originate from an outside source over the link. Local
inference is the compliant form; it's a larger build (model + hardware) and is deferred.

---

## 5. Action items

1. **Before any race use, confirm with the OA/RC in writing** how they read §2.1(d) for an
   onboard/AI navigator (and, if pursuing the public-feed theory in §3, that exact proposal), and
   re-check the **Sailing Instructions** when published — they can change the NOR and take precedence.
2. **Default to safe:** in a race with the cloud agent, restrict to passive collection + safety +
   own-data; use full coaching only for **practice, deliveries, and debriefs**.
3. **Recommended code follow-up:** make Race mode a **server-side** compliance gate (see §4A), not
   just a UI gate, with an explicit "racing — outside tactical help withheld (RRS 41)" response.
4. **Longer term, if full in-race coaching is wanted:** pursue the **all-onboard, local-model**
   path (§4B).
5. Carriage of the **position transponder** and the **primary-nav-GPS finish/gate photos** are
   required by the NOR (§2.1(f), §8) — orthogonal to RRS 41, but note the boat must carry/operate
   them.

---

*Sources: World Sailing RRS 2025–2028 Rule 41 (racingrulesofsailing.org); 2026 Bayview Mackinac
Race Notice of Race, `2026NOR V6 111925 Approved_Post` (bycmack.com). Re-verify against the
as-published Sailing Instructions before the race.*
