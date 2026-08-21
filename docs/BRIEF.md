# Handoff: Voice-First Nutrition Logger — System Design Brief

**Status:** Pre-design. No code exists.
**Task:** Produce the system design. Do not write application code yet.
**Date:** 2026-08-20

---

## 1. What this is

A personal, single-user PWA that lets Bill log food by **talking**, gets back an **editable list of items**, and commits them to a diary he owns. Deployed on Vercel.

It is an experiment first and a product second. The hypothesis being tested is narrow and specific:

> Does a logger that learns your personal eating history get fast enough, fast enough, to change whether you keep logging at all?

Every existing tracker is exactly as tedious on day 200 as on day 1. This one should get measurably faster every week. Whether that's true is the thing we're finding out — so **instrumentation is a first-class requirement, not an afterthought** (see §8).

---

## 2. Pain points — the actual source of every requirement

These are Bill's own words from the conversation that produced this brief. Design against these directly; they are the spec.

### The core complaint

> "i have been using cronometer cause i have to click buttons and select when and portions, etc. talking is so much easier"

That is **three distinct frictions**, and they have three different solutions. Do not collapse them:

| # | Friction | What it means | Fix |
|---|---|---|---|
| 1 | **Clicking buttons** | Searching a database and tapping through result lists for every item | Voice/text → LLM decomposition into items |
| 2 | **Selecting *when*** | Having to pick the meal (breakfast/lunch/dinner/snack) and time on every entry | Infer from clock, pre-fill, keep editable. **No AI needed.** |
| 3 | **Selecting portions** | Having to specify quantity and unit every single time for foods he eats constantly | Learned defaults from his own logging history. **No AI needed.** |

**This table is the most important thing in this document.** Two of the three frictions are solved by *defaults and memory*, not by model quality. The app can feel dramatically better than Cronometer even if the LLM decomposition is only adequate. Do not architect as though parse quality carries the experience.

### The core interaction he asked for

> "the most important part should just be being able to say something, it comes back with a list of items, and then you can modify it as needed"

Propose → **editable staging area** → commit. Nothing is written to the diary until he confirms. The confirm step is a feature, not a failure mode — corrections are training data that improve future defaults.

### The personalization cases

> "for example I should be able to say i ate a fairlife and it finds a fairlife, or i say something like i have my usual breakfast"

These are **two different problems** and neither is a database search:

- **"a fairlife"** — brand shorthand. Globally ambiguous (Fairlife makes Core Power, Nutrition Plan, 2% milk, chocolate milk, multiple sizes). For *him* it's unambiguous because he drinks the same one. Resolve from **his history**, not the global database.
- **"my usual breakfast"** — a stored multi-item meal macro. Pure expansion, no search, no LLM.

### Other stated requirements

- **"i want history tracking and a dashboard"** — in this app. He is not keeping another app around for this.
- **Real database entries, not LLM-estimated nutrition.** He explicitly wants a large real food database ("the 1.5m SKUs"). The LLM identifies food; it must **never author nutrition numbers**. He tracks to a precise target and invented macros would corrupt the data.
- **Hates Cronometer's photo logger and voice logger.** He has used both. Whatever they do, do it differently — specifically, they don't give him a good editable multi-item list, and they don't know his personal history.

### Context on the user

- Training hard, targeting **180g protein/day**. That's the number the dashboard should make impossible to ignore.
- Diet is **repetitive** — likely 20–30 foods cover ~95% of intake. This is why the memory layer works, and why the cold-start problem (§7) matters less for him personally than it would for a public product.
- Comfortable technically; will be the only user for the foreseeable future.

---

## 3. Established decisions — do not re-litigate these

These were settled through research during the conversation. Rationale included so you can evaluate, but the burden of proof is on changing them.

### Food database: FatSecret Platform API

Chosen after evaluating Cronometer, MyFitnessPal, USDA FoodData Central, Open Food Facts, Nutritionix, Edamam, and Spoonacular.

- **Basic tier is free**: 5,000 calls/day, US dataset. Bill will use ~30/day, and the alias layer drives that toward zero for repeat foods.
- 1.9M+ foods, 90%+ barcode coverage, dietitian-verified.
- Has an **official, documented API** — unlike Cronometer (no public API, and its ToS §10(b) explicitly prohibits automated access) and MyFitnessPal (private partner API, developer program closed to new applicants since 2019).
- OAuth 1.0.

**Why not the alternatives:** Cronometer would have required reverse-engineered endpoints that break on their release schedule and violate ToS. Nutritionix has the best US restaurant coverage but appears to have discontinued free non-commercial access. Edamam's branded/UPC coverage is too weak for packaged goods like fairlife and Quest bars. USDA FDC is excellent for whole foods and free — **worth considering as a supplementary source** for lab-analyzed micronutrient depth on staples (chicken, rice, eggs), which is a genuine open question to resolve.

### Storage: our own, not FatSecret's diary

FatSecret's API *can* write to a user's diary (`food_entries` create/edit/delete). We are **not** using that, because:
- He wants the dashboard and history in this app.
- It avoids 3-legged OAuth user delegation; we only need 2-legged app auth for food search, which is much simpler.
- We own the data outright.

Use FatSecret as a **food data source only**. Cache every resolved food's full nutrition payload locally so repeat logs never hit the API.

### Model: Claude Opus 5 (`claude-opus-5`)

- Use **structured outputs** (`output_config: { format: {...} }`) so decomposition returns a validated schema, not prose to parse.
- Cost is negligible: ~500 input / ~200 output tokens per text log ≈ **$0.0075/log**, under $2/month at his volume. Do not optimize for token cost; optimize for getting grams right.
- Use the official `@anthropic-ai/sdk`.

---

## 4. The resolver — this is the whole product

Capture is a commodity. The dashboard is a commodity. **The resolver is the only novel part**, and it's where design effort should concentrate.

A tiered cascade. Only the last tier touches the network:

| Tier | Input example | Resolved by | Hits FatSecret? | Target latency |
|---|---|---|---|---|
| 0 | "my usual breakfast" | stored meal macro → expands to N items | no | instant |
| 1 | "a fairlife" | personal alias table, exact/normalized match | no | instant |
| 2 | "protein shake" (novel phrasing, known food) | semantic match over *his own* resolved foods | no | instant |
| 3 | "the pesto thing from Sweetgreen" | LLM-driven search against FatSecret | **yes** | 3–8s, once ever |

**Critical design principle: the LLM search is the cold-start path, not the hot path.** Putting it in the hot path rebuilds the thing he hates — waiting. It also introduces non-determinism: an agent freely choosing among search results will pick a *different* database entry for "chicken breast" on different days, which silently corrupts longitudinal data. Tiers 0–2 are deterministic by construction.

Every tier-3 resolution **writes a new alias**, so any given food takes the slow path at most once. This is the compounding mechanism the entire hypothesis rests on.

### Tier 3 ranking rules

A naive `search("chicken breast")` returns a mess. The resolver needs criteria:

- **Generic/whole food** ("chicken breast", "broccoli") → prefer lab-analyzed entries with complete nutrient profiles.
- **Branded SKU** ("Fairlife Core Power") → prefer the branded entry, even though label-derived entries carry fewer nutrients. Substituting a generic for a specific product is *worse* data, not better. This is the rule a naive implementation gets backwards.
- **Cooked vs raw must be disambiguated.** 100g raw chicken and 100g cooked chicken differ by roughly a third in protein density. This is the single most common way people silently miss a protein target.
- **Allow query refinement.** "protein bar" → junk results → infer or ask the brand → "Quest chocolate chip cookie dough" → clean hit. That refine loop is the only real justification for an agentic approach here; everything else is a lookup.

### Ambiguity handling

When "a fairlife" matches two things he's logged before, **do not silently guess and do not dump a search list.** Ask with the usual pre-selected:

> Core Power Elite chocolate (usual, 60×) or 2% milk?

One tap. The tap is training data.

---

## 5. Known technical blocker — read before choosing the capture approach

**The Web Speech API does not work inside an installed iOS PWA.**

It feature-detects as present — so naive detection will report success — and then does nothing. It works in a regular Safari tab but not in a home-screen standalone app. There are also documented `getUserMedia`/`MediaRecorder` reliability problems in iOS standalone mode (WebKit bug 185448; reports of recording working on first launch then failing after close/reopen until device restart).

Android/Chrome has no equivalent limitation.

**Implications to design around:**

1. **Do not build on `webkitSpeechRecognition`.** The robust path is `MediaRecorder` → audio blob → upload → server-side transcription → LLM decomposition.
2. **Text input must be a first-class path, not a fallback.** Typing "two eggs and a fairlife" runs through the identical decomposition → resolve → confirm pipeline. This means iOS audio flakiness degrades to typing rather than breaking the app.
3. Confirm Bill's actual device before finalizing. He has an Apple Developer subscription and asked about Apple Health, so iPhone is likely — but verify.

This constraint is the reason to design the pipeline so that **transcription is a swappable front-end stage**, not baked into the capture component.

---

## 6. Stack constraints

- **Vercel deployment.** Next.js App Router is the natural fit; deviate only with a reason.
- **PWA**: installable, manifest, service worker, works on his phone.
- **TypeScript.** (Note: the surrounding repo is Python, but this is a web app — don't force Python.)
- **Single user.** Auth can be trivial (a shared secret, a single-user session). Do not build multi-tenancy.
- **Persistence**: needs to work on Vercel. Postgres (Neon is on the Vercel marketplace) is the obvious candidate; evaluate alternatives for a single-user app.
- Secrets in environment variables. Never commit the FatSecret consumer key/secret or the Anthropic API key.

---

## 7. Explicit non-goals

Ruled out by Bill directly. Do not design for these:

- ❌ **Apple Health / HealthKit sync.** Explicitly deferred — "ignoring the complicated health app syncing issue."
- ❌ **Native app, App Store, TestFlight.** PWA only.
- ❌ Multi-user, accounts, sharing, social.
- ❌ **Photo logging.** Voice/text first. Design so photo *could* plug into the same decompose→resolve→confirm pipeline later, but do not build it.
- ❌ Barcode scanning (same — later, same pipeline).
- ❌ Cronometer integration of any kind.
- ❌ Solving cold-start for a general public audience. His diet is repetitive and he'll bootstrap his own aliases within two weeks. (Noted because cold-start *is* the hard problem if this ever becomes a product — but it is not this build's problem.)

---

## 8. Instrumentation — required, not optional

Bill said "i will use it to voice log food for myself for now, and see what happens." The app must be able to answer *what happened*. Three metrics, recorded from day one:

| Metric | Definition | Why |
|---|---|---|
| **Log latency** | Seconds from capture start → committed entry | The core UX claim. Should trend down. |
| **Alias hit rate** | % of items resolved at tiers 0–2 with no network call | The compounding curve, made visible. Should trend up. |
| **Adherence** | Days logged per week | The actual outcome. Compare against his Cronometer baseline. |

Store these as first-class data, not logs. A small internal view showing all three over time is worth more than most dashboard features, because it's the evidence that decides whether this idea has legs.

---

## 9. Open decisions to resolve

Genuinely unresolved. Make a recommendation with reasoning:

1. **Transcription provider** — server-side Whisper, a hosted STT API, or something else. Constraints: runs on Vercel, low latency, handles food/brand vocabulary reasonably.
2. **Supplement FatSecret with USDA FoodData Central?** Free, official, 1,000 req/hr, lab-analyzed with full micronutrient panels — better than FatSecret for whole-food staples. Worth the second integration, or unnecessary complexity?
3. **Tier 2 semantic matching** — embeddings (which provider, stored where) vs. fuzzy string matching. For a personal corpus of ~50 foods, is a vector store overkill?
4. **Data model for meal macros** — how "my usual breakfast" is defined, edited, and versioned when his usual breakfast changes.
5. **Portion default strategy** — most recent, mode, or time-weighted? What happens when he logs an unusual quantity once?
6. **Offline behavior** — does capture work without network, queuing for later resolution? Matters more than usual because he'll be logging in gyms and restaurants.
7. **Persistence choice** and schema for: `foods` (cached nutrition payloads), `aliases`, `meal_macros`, `entries`, `metrics`.

---

## 10. Deliverable

A system design document covering:

- Architecture diagram (capture → transcribe → decompose → resolve → confirm → persist)
- Data model with concrete schemas
- The resolver cascade as a precise algorithm, including tier-3 ranking rules and the ambiguity/disambiguation flow
- API surface (internal routes)
- Recommendations with reasoning on all seven open decisions in §9
- Instrumentation plan per §8
- Build sequence — what to ship first to get Bill logging real meals soonest

**Build order bias:** the fastest path to evidence is text input → decomposition → resolve → confirm → persist, with voice added as a front-end stage once that loop works. Bill can type for a week; he cannot evaluate a resolver that doesn't exist.

---

## Appendix: research already done

Findings from the conversation that produced this brief. Don't re-research these.

- **Cronometer** — no public API. ToS §10(b) prohibits scraping/automated scripts; §1(g) permits termination without notice. Reverse-engineered GWT and mobile endpoints exist (`gocronometer`, `cronometer-api-mcp`) but break on their releases. Ruled out.
- **MyFitnessPal** — private partner API (`api.myfitnesspal.com` v2) does read/write diary + webhooks, but the developer program is closed to new applicants. Unavailable.
- **MacroFactor** — closest existing product to the target UX. Their "Plate" is a staging area with editable per-item rows; MacroFactor AI uses "several LLM prompts in tandem" to decompose meals and query their database for real foods. Their docs conflict on whether the voice/text "AI Describe" path is LLM-backed or plain search, and their help docs state it does **not** search a user's personal library. **That gap — personal history resolution — is this project's entire differentiation.**
- **Open-source prior art** — `fud-ai` (MIT, 351★, 13 AI providers incl. Claude, 9 input methods, native iOS/Android) is worth mining for capture-layer patterns. `nutritrace` (AGPL, 205★) has the closest propose-and-confirm flow. Most others (`caloriemate`, `nom.ai`, Nutritheous) let the **LLM estimate nutrition directly with no database** — the failure mode we're explicitly avoiding.
- **Market context** — a full market analysis exists as an artifact. Short version: the category rewards distribution over technology (Cal AI: $30M ARR, ~9 people, won on TikTok micro-influencers, sold to MyFitnessPal for high eight figures), voice ranks last among gen-AI interfaces (<1 in 5 users), and memory-as-differentiator is already claimed by funded competitors (KCALM, Welling, Macaron). None of that blocks a personal build — it's why this is an experiment rather than a startup.
