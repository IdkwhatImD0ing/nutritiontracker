# Onboarding — start here after cloning

**Tally** is a single-user, voice-first nutrition logger: say (or type) what you ate → a
pre-filled editable list → one tap to commit. It learns your foods, portions and phrasings,
so the same meal gets faster every week.

Everything below was verified on 2026-09-03 at the Phase 5 commit (the one titled "Phase 5 (M4)…" in `git log`).

---

## 1. One-page orientation

| | |
|---|---|
| **Stack** | Next.js 16 (App Router, TS) PWA + Python FastAPI, deployed as **two Vercel Services on one origin** |
| **Database** | Neon Postgres (`pg_trgm`, `pgvector` available) — Vercel-Marketplace-managed |
| **Auth** | Neon Managed Better Auth; Next holds the session cookie, the client sends a 15-min EdDSA JWT, FastAPI verifies it via JWKS |
| **LLM** | OpenAI Agents SDK — `gpt-5.6-luna` (decompose), `gpt-5.6-terra` (tier-3 agent). **Always pin the full model id**, never bare `gpt-5.6` |
| **Speech** | Deepgram `nova-3` primary, OpenAI `gpt-transcribe` fallback |
| **Food data** | USDA FDC (generic + branded + barcodes) primary, Open Food Facts quality-gated fallback, chain restaurants only via web transcription with a stored source URL |
| **Tracing** | Langfuse Cloud (Python SDK v4), prod/test split by `LANGFUSE_TRACING_ENVIRONMENT` |
| **Types** | Defined **once** in pydantic → OpenAPI → generated TypeScript. Never hand-edit `frontend/lib/api-types.ts` |

**Read [DESIGN.md](DESIGN.md) before changing behavior.** It is the authoritative spec, not a
historical document — §3 (resolver cascade), §4 (schema), §7 (product surface) and §8
(decisions, with the *why*) answer most design questions. [BACKLOG.md](BACKLOG.md) is the
living findings ledger: fixed items are struck through, deferred items say why.

---

## 2. Setup (~5 minutes)

Prerequisites: **Python ≥ 3.12** with [`uv`](https://docs.astral.sh/uv/), **Node** with `pnpm`,
and the [Vercel CLI](https://vercel.com/docs/cli) (`npm i -g vercel`).

```bash
git clone git@github.com:IdkwhatImD0ing/nutritiontracker.git && cd nutritiontracker
```

```bash
vercel link
```

Pull the secrets — every key the app needs lives in Vercel's **Development** environment, so
you never have to collect them by hand:

```bash
vercel env pull .env --environment=development --yes
```

The frontend reads its own env file, so copy it across (Next does not read the repo-root `.env`):

```bash
cp .env frontend/.env.local
```

Install both sides:

```bash
cd backend && uv sync && cd ../frontend && pnpm install
```

**`.env` and `.env.local` are gitignored and must stay that way.** They have never been
committed — keep it that way. `.env.example` lists the canonical variable names with no values.

### What you just pulled

`DATABASE_URL`, `NEON_AUTH_BASE_URL`, `NEON_AUTH_COOKIE_SECRET`, `OPENAI_API_KEY`,
`DEEPGRAM_API_KEY`, `FDC_API_KEY`, `OFF_USER_AGENT`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `LANGFUSE_TRACING_ENVIRONMENT`, plus the
Neon-managed `POSTGRES_*`/`PG*` set.

Two deliberate omissions:

- **`APP_ORIGIN` is Production-only.** It pins the Origin check on state-changing requests.
  Unset locally, `backend/auth.py` falls back to a dev-friendly localhost allowance — setting
  it locally will 403 your own requests.
- **`ALLOWED_EMAIL`** is optional; `auth.py` hardcodes the same default. This app is
  single-user by design — the allowlist is one address.

The schema is already applied to the shared Neon database. `backend/migrations/apply.py` is a
no-op against an initialized database and aborts on an unexpected one, so it is safe to run.

---

## 3. Running it locally

```bash
uv run --directory backend uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
pnpm --dir frontend dev --port 3000
```

Open **http://localhost:3000** and sign in. `.claude/launch.json` defines both servers if your
tooling reads it.

In deployments `vercel.json` routes `/api/*` to FastAPI; `next dev` has no such router, so
`frontend/next.config.ts` adds a **development-only** rewrite to `127.0.0.1:8000` (it returns
`[]` outside development, so production routing is untouched). Override the target with
`DEV_API_URL` if you run the backend elsewhere.

---

## 4. Verifying a change

Run all four before claiming anything works:

```bash
cd backend && uv run --with pytest pytest tests -q -m "not live"
```

```bash
cd backend && uv run --with pytest pytest tests -q -m live
```

```bash
cd frontend && pnpm run typegen && pnpm exec tsc --noEmit && pnpm run build
```

```bash
cd frontend && pnpm run typegen:check
```

Notes that will save you an hour:

- **DB tests run against the production Neon database inside force-rollback transactions.**
  There is no dev branch yet (BACKLOG 18) — creating one needs Console access, because the
  project is Vercel-Marketplace-managed and invisible to the Neon API token. Keep the
  rollback pattern and leave no rows behind.
- `-m live` spends real API credits and hits real vendors.
- `test_tier3_live.py::test_quest_bar_branded_or_question` is a **real model run and is
  nondeterministic** — it fails occasionally. Re-run once; never weaken the assertion to get
  green. The same file also cannot be collected on its own (it installs `sys.modules` stubs);
  run it with the rest of the suite.
- **Changing a pydantic model changes the API contract.** Re-run `typegen` or the frontend
  will type-check against a stale schema.

---

## 5. Rules that are not negotiable

These came from the project owner or from bugs that already bit. Breaking them is a
regression even when tests pass.

1. **Never commit `.env`, secrets, or credentials.** Never print a secret value — names only.
2. **Estimation is gated off everywhere except the photo lane.** Text and voice resolve
   against a database or a stored source URL; if a food cannot be resolved, say so. A
   post-run evidence gate enforces this in code — do not route around it.
3. **Offline is a deliberate non-feature.** No queueing, no IndexedDB drain. Without a
   network, capture is disabled with a clear message.
4. **Tier-0 (saved-meal) rows never teach anything.** No aliases, no portion defaults — the
   saved meal *is* the memory. Every rewrite site preserves `tier = 0`.
5. **Every rewrite of `item_resolutions.staged` must append `KEEP_LOG_KEYS_SQL`.** One missing
   site silently dropped the diary-date shift and billed back-dated meals to today
   (BACKLOG 57). If you write a new one, preserve the log-level keys.
6. **Never fail silently.** A saved meal missing a food says so; a voice clip with no audio
   says so and offers the text box. Half-correct numbers the user believes are whole are the
   worst outcome in this app.
7. **Agents deploy previews only** (`vercel deploy --yes`). Production promotion is a human
   decision.

---

## 6. Where things are

```
backend/
  main.py              FastAPI app; every router is registered here
  models.py            pydantic contracts — the single source of truth for API types
  auth.py              JWT verification, allowlist, Origin check
  db.py  tracing.py    connection pool; Langfuse setup
  resolver/
    cascade.py         the resolver cascade: tier 0 → 1 → 2 → 3, and macro expansion
    commit.py          §3.5 idempotent commit transaction, alias/portion learning
    norm.py            phrase normalization (used by every tier)
  services/
    decompose.py       utterance → items (Luna, structured output)
    tier3.py           the OpenAI-Agents food-resolution agent + its tools
    transcribe.py      Deepgram/OpenAI behind one interface
    context.py         what the LLM is told about your history
  sources/             fdc.py (USDA), web.py (restaurant transcription lane)
  routes/              one module per API surface
  evals/               Langfuse dataset collection + replay harness
  tests/               pytest; `-m live` marks the ones that spend money
frontend/
  app/                 App Router pages: /, /history, /library, /settings, /experiment
  components/          UI + per-surface API seams
  lib/api-types.ts     GENERATED — never edit by hand
docs/                  DESIGN.md (spec) · BUILD_PLAN.md (phases) · BACKLOG.md (ledger) · BRIEF.md
```

---

## 7. Current state

**All five phases are built, tested and committed — M0 through M4 are complete**: the core
loop, the friction killers, voice + PWA, saved meals + Library + History + goal wizard +
`/experiment` + the eval harness, and the photo lane plus the bottom **+** with its four ways
to log (barcode · photo · search · voice; DESIGN §3.4b, §7.4). Phase 5 went through three
fix waves, each driven by adversarial verifiers reproducing bugs against the live database
in rolled-back transactions before anything was fixed (BACKLOG 72–98 is the record; the
commit-gate bypasses in 79, 91 and 92 are worth reading before touching `resolver/commit.py`).
At the Phase 5 commit: 224 non-live backend tests pass, 21 live (the known xfail xpassed),
frontend type-checks and builds clean, typegen is drift-free. Two tests in
`test_tier3_live.py` are not `live`-marked but hit a real model / real FDC latency and can
flake (BACKLOG 60, 71) — re-run once, never weaken.

What comes next is the backlog, not a phase: the iPhone spike (below), the Neon dev branch
(18), the preview-protection bypass token (19), the input-hardening pass (98), and whatever
real use turns up.

**M4's gate — a photographed plate stages with resolved staples and flagged estimates — is
met with vision mocked** (`tests/test_e2e_m4.py`) and exercised once for real by the single
live vision test on a CC0 photo (`tests/test_photo.py`, 8 items identified in ~3 s, no
nutrition numbers in the output). No agent has pointed a phone at a plate. Photo is the
*only* place estimation is allowed; every estimated value is visually flagged and commit
refuses it server-side until it is confirmed (§3.4b). An item nothing resolves is
`estimated: "unresolved"` — a numberless row that shows what the photo saw and routes to
Search; it can never confirm or commit, and the app never invents calories (BACKLOG 63,
settled).

Two open threads a new agent should know about:

- **The iPhone spike is still unrun** (§12 M2, §13). Install the PWA, record, fully close the
  app, reopen, record again — iOS is documented to break exactly there, sometimes *silently*
  (a live-looking track that yields a silent blob). Its outcome decides whether voice stays
  MediaRecorder-in-PWA, falls back to a Safari tab, or relies on keyboard dictation. Only a
  human with the device can run it.
- **Preview deployments are behind Vercel deployment protection** (BACKLOG 19), so no agent
  has rendered the M3 UI in a browser — that confidence rests on type-checking and the build.
  A Protection Bypass token would unblock it.
