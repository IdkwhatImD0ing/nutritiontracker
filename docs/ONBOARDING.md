# Onboarding — start here after cloning

**Tally** is a voice-first nutrition logger: say (or type) what you ate → a pre-filled
editable list → one tap to commit. It learns your foods, portions and phrasings, so the same
meal gets faster every week. One account owns an instance; every other sign-up is a **demo
account** (one log a day), and the database keeps each account's rows apart (DESIGN §2, §8.9).

Everything below was verified on 2026-09-03 at the Phase 5 commit (the one titled "Phase 5 (M4)…" in `git log`).
The tenancy/demo material of the same day (the notes in §2 and §4, rule 8 in §5, the last paragraph of §7, all of §8)
was written from the code as it landed, and §8 has not been walked from an empty Neon project.

---

## 1. One-page orientation

| | |
|---|---|
| **Stack** | Next.js 16 (App Router, TS) PWA + Python FastAPI, deployed as **two Vercel Services on one origin** |
| **Database** | Neon Postgres (`pg_trgm`, `pgvector` available) — Vercel-Marketplace-managed. Row-Level Security scopes every user-owned table to `app.user_id`; `foods` is shared |
| **Auth** | Neon Managed Better Auth; Next holds the session cookie, the client sends a 15-min EdDSA JWT, FastAPI verifies it via JWKS. Any verified account may sign in: `ALLOWED_EMAIL` is the owner, everyone else is a demo (`GET /api/me` says which) |
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

This is the owner's path — the secrets come from the owner's Vercel project. Running your own
instance from your own keys is §8.

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
- **`ALLOWED_EMAIL`** names the **owner** account — the one with no daily cap. Unset,
  `backend/auth.py` falls back to the original author's address, which is right on the
  owner's machines and wrong on yours (§8). Nothing in the frontend reads it: sign-up is
  open to any address, and the API decides who the owner is.

The schema is already applied to the shared Neon database. `backend/migrations/apply.py`
(001) is a no-op against an initialized database and aborts on an unexpected one, so it is
safe to run. Migration 002 (tenancy — `user_id` columns + Row-Level Security, DESIGN §4) is
applied by the owner **by hand** after review, never by an agent; its dry run happens inside
a transaction that is rolled back, the same way the tests run. **Order matters:** apply 002
(dry-run, apply, `--check`) *before* pushing `main` — the deployed backend runs every
statement as the `tally_app` role that only 002 creates, so a push first means every
DB-touching request fails until 002 lands (fail-closed, not a leak — but an outage).

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
- **Every query runs as a user.** The database enforces tenancy (RLS on the user-owned
  tables; `foods` is shared), keyed on the `app.user_id` setting and the `tally_app` role that
  `backend/db.py` binds at the top of every transaction from the JWT's `sub`. With no user
  bound, selects return nothing and inserts fail — so `tests/tenancy_harness.py`
  (`bind_test_tenant`) runs right after each `isolated_db` opens its rollback transaction: it
  applies 002 inside that transaction while the database is still pre-002 (the suite is the
  migration's dry run, and keeps passing after the real apply), switches to the app role,
  binds the fixed tenant `test-user`, seeds the 001 `settings` rows for it, and marks the
  in-process user as the owner. `TestClient` tests override `auth.require_user` with
  `override_user(...)`, never a bare lambda — a sync override runs in a worker thread whose
  context is discarded. An empty result in a new test is the first thing to check.
  `tests/conftest.py` imports `services.quota` before any test stubs the `services` package,
  so the routes' quota checks keep resolving under the stub.
- **After any deploy, check the auth round-trip, not just `/api/health`.**
  `curl -s https://<host>/api/auth/token` must answer Better Auth's
  `{"message":"Unauthorized",...}` (401). If it answers `{"detail":"Not Found"}`
  that is FastAPI replying, `/api/auth/*` is misrouted, and **no client can get
  a JWT** — every authenticated call fails while `/api/health` still looks fine.
  `/api/auth/*` belongs to the *web* service (`app/api/auth/[...path]`); every
  other `/api/*` path is FastAPI's. `vercel.json` orders those two rules, and
  `next.config.ts` does the same job locally with a `fallback` rewrite (BACKLOG 130).
- **`next build` and `next dev` share `.next`.** Running a production build
  between dev sessions leaves artifacts the next `next dev` serves instead of
  compiling: routes 404 that plainly exist, and `/api/auth/*` falls through to
  the backend. If the app is 404ing on a page whose file is right there, stop the
  dev server, delete `frontend/.next`, and start it again.
- `-m live` spends real API credits and hits real vendors.
- `test_tier3_live.py::test_quest_bar_branded_or_question` is a **real model run and is
  nondeterministic** — it fails occasionally. Re-run once; never weaken the assertion to get
  green. The same file also cannot be collected on its own (it installs `sys.modules` stubs);
  run it with the rest of the suite.
- **Changing a pydantic model changes the API contract.** Re-run `typegen` or the frontend
  will type-check against a stale schema.

---

## 4b. Changing the domain

Three things are pinned to the hostname; miss the second and every write fails
with a 403 that looks nothing like a DNS problem.

1. **Vercel** — add the domain to the project and let it issue a certificate.
2. **`APP_ORIGIN`** (Production env) — `backend/auth.py` refuses a
   state-changing request whose `Origin` does not match it. It accepts a
   comma-separated list, so set **both** origins during the cutover
   (`https://old.example,https://new.example`), deploy, move DNS, then drop the
   old one. Reads keep working throughout; it is only POST/PATCH/DELETE that
   are checked, which is why the symptom is "the app loads but nothing saves".
3. **Neon Auth trusted domains** — Console → your project → Auth. The session
   cookie and the redirect targets are bound to the origin; a domain that is not
   on the list can sign in and immediately appear signed out.

Optional but worth it: `NEXT_PUBLIC_SITE_URL` sets `metadataBase`, which makes
the Open Graph and Twitter URLs absolute so link previews resolve. It defaults
to the canonical domain in `app/layout.tsx`.

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
7. **Pushing `main` deploys production.** The Vercel project is connected to the GitHub repo
   and auto-deploys every push to `main` (`vercel project inspect` does not print the git
   link — the deployment metadata's `source: "git"` does). Agents deploy previews only
   (`vercel deploy --yes`); production changes only through a push the owner asked for.
8. **Tenancy is the database's job.** Never add `WHERE user_id = …` by hand, never touch the
   RLS policies to make a query work, and never run request code without `app.user_id` set.
   A new table that holds user data gets the same `user_id` default + policy as the tables in
   migration 002 (the app role's privileges follow from 002's default privileges). Code with
   no request (evals, migrations) binds the owner explicitly.

---

## 6. Where things are

```
backend/
  main.py              FastAPI app; every router is registered here
  models.py            pydantic contracts — the single source of truth for API types
  auth.py              JWT verification, owner-vs-demo, Origin check
  db.py  tracing.py    connection; binds tally_app + app.user_id per transaction; Langfuse setup
  migrations/          001_init.sql, 002_multiuser.sql, apply.py (`002 --dry-run` verifies + rolls back)
  resolver/
    cascade.py         the resolver cascade: tier 0 → 1 → 2 → 3, and macro expansion
    commit.py          §3.5 idempotent commit transaction, alias/portion learning
    norm.py            phrase normalization (used by every tier)
  services/
    decompose.py       utterance → items (Luna, structured output)
    tier3.py           the OpenAI-Agents food-resolution agent + its tools
    transcribe.py      Deepgram/OpenAI behind one interface
    context.py         what the LLM is told about your history
    quota.py           demo quota: one log a day, ten paid re-resolutions (§5 of DESIGN)
  sources/             fdc.py (USDA), web.py (restaurant transcription lane)
  routes/              one module per API surface (me.py: GET /api/me)
  evals/               Langfuse dataset collection + replay harness (runs as the owner)
  tests/               pytest; `-m live` marks the ones that spend money; tenancy_harness.py
                       binds every isolated_db to a tenant
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

**2026-09-03 — the repo went public.** Tenancy landed first: migration 002 adds `user_id` to
the six user-owned tables (backfilled to the owner) and turns on Row-Level Security, so a
second account cannot see the owner's diary by construction; `foods` stays a shared cache.
Any verified account may sign in; the `ALLOWED_EMAIL` address is the owner and everyone else
is a demo — one log per UTC day across every lane and ten paid re-resolutions, a 429 with the
reset time past that (DESIGN §5, §8.9); `GET /api/me` tells the client which it is, and Today
shows a demo the banner. Migration 002 is applied by the owner by hand (`apply.py 002
--dry-run`, then `apply.py 002`, then `--check`) — and **before** the push that deploys this
tree, because the deployed backend needs the `tally_app` role 002 creates;
`tests/test_tenancy.py` is its rehearsal in the suite. The review pass the same day moved the
demo log quota onto an attempt ledger (`quota_events`, DESIGN §5) and made the shared
`foods` cache owner-labelled (DESIGN §8.9). A second adversarial pass then made the ledger
atomic (002's partial unique index admits the day's one attempt; the database's clock judges
the day), moved every lane's claim ahead of STT and vision, took the web tools away from a
demo's tier-3 agent, made photo placeholders visible to the photographer alone, and restored
the voice lane's idempotent `capture_failure` replay that the claim-first change had broken
(BACKLOG 117, 119, 121–128; `tests/test_quota_hardening.py`). At this tree, before the apply:
239 non-live tests pass and 3 skip until 002 is applied (the real-path alias learning in
`test_commit_hardening` and `test_resolver`, and `test_tenancy` 7); live, 19 pass, 2 skip for the
same reason (`test_e2e_m1`, `test_e2e_m3`) and the known xfail xfails; typegen is drift-free,
`tsc` and `next build` are clean.

What comes next is the backlog, not a phase: the iPhone spike (below), the Neon dev branch
(18, now 100), the preview-protection bypass token (19), the input-hardening pass (98), the
open-sourcing follow-ups (99–111), and whatever real use turns up.

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

---

## 8. Running your own instance

Everything in §2 assumes you can `vercel env pull` from the owner's project. You cannot, so
this is the from-scratch path: your own keys, your own Neon project, your own Vercel project.
It was written alongside the tenancy change and has **not** been walked end to end from an
empty Neon project — if a step is wrong, fix the doc.

### 8.1 Accounts and keys

Everything but OpenAI runs on a free tier; OpenAI meters usage, roughly $1–2 a month for one
person. DESIGN §0.5 has the *why* for each.

| Need | Where | Env var(s) |
|---|---|---|
| Neon project | [neon.tech](https://neon.tech); enable **Auth** on the branch (Console → Project → Branch → Auth) | `DATABASE_URL` (the **pooled** `-pooler` string), `NEON_AUTH_BASE_URL` |
| Cookie secret | generate one: `openssl rand -base64 32` | `NEON_AUTH_COOKIE_SECRET` |
| Your owner address | the email you will sign up with | `ALLOWED_EMAIL` |
| OpenAI | an API key with access to the GPT-5.6 family and the web search tool | `OPENAI_API_KEY` |
| Deepgram | an API key — or set `STT_PROVIDER=openai` and skip it; OpenAI transcribe is the fallback either way | `DEEPGRAM_API_KEY` |
| USDA FDC | free key: https://fdc.nal.usda.gov/api-key-signup | `FDC_API_KEY` |
| Langfuse | a free Hobby project. `backend/tracing.py` builds the client at startup; how the SDK behaves with no keys is unverified and a project takes two minutes, so treat the keys as required | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`, `LANGFUSE_TRACING_ENVIRONMENT=development` |
| Vercel | a project of your own; **Vercel Services** (beta) must be enabled on the account — `vercel.json` depends on it | — |

`OFF_USER_AGENT` is in `.env.example` for the day an Open Food Facts adapter lands (DESIGN
§8.2); nothing reads it today.

### 8.2 Env files

```bash
git clone https://github.com/IdkwhatImD0ing/nutritiontracker.git && cd nutritiontracker
cp .env.example .env            # fill in every value above
cp .env frontend/.env.local     # Next reads its own file
cd backend && uv sync && cd ../frontend && pnpm install
```

Leave `APP_ORIGIN` unset locally (§2). Never commit either file.

### 8.3 Database

Two migrations, in order, from the repo root, against the `DATABASE_URL` in `.env`:

1. **001 — the schema.** Guarded: applies only to an empty `public` schema, no-op when the
   seven tables already exist, aborts on anything else.

   ```bash
   uv run --directory backend python migrations/apply.py
   ```

2. **Sign up as the owner** before 002: start both servers (§3), open
   http://localhost:3000/auth/sign-up and register with the `ALLOWED_EMAIL` address (the
   backend reads it from `.env`; the sign-up page itself accepts any address). Sign-up
   writes only to the auth service (users live in the `neon_auth` schema of your
   database); 002 needs that one user to exist. Let nobody else sign up before 002 is
   applied — until then nothing separates accounts. Only sign-up works at this point: the
   API errors on every DB-touching request until step 4, because this tree's backend runs
   as the `tally_app` role that 002 creates.

3. **Point 002 at your address.** The owner lookup is hardcoded to the original author's
   email in two places — the `DO` block at the top of `backend/migrations/002_multiuser.sql`
   and `OWNER_EMAIL` in `backend/migrations/apply.py` — and does not read `ALLOWED_EMAIL`
   (BACKLOG 109). Change both to your address first, or 002 aborts with *expected exactly
   one Neon Auth user for the owner, found 0*.

4. **002 — tenancy.** Adds `user_id` to `aliases`, `meal_macros`, `logs`, `item_resolutions`,
   `entries` and `settings`, backfills every existing row to the owner's Neon Auth user,
   scopes `settings` and `aliases` uniqueness by user, adds the `quota_events` ledger and the
   `tally_app` role the app runs as, and turns on Row-Level Security (DESIGN §4). Guarded
   like 001 (refuses unless exactly the seven 001 tables exist; no-op once `logs.user_id`
   exists) and verified inside the same transaction before it commits. Dry-run first — it
   applies, checks, and rolls back:

   ```bash
   uv run --directory backend python migrations/apply.py 002 --dry-run
   ```

   Then for real, once, and confirm it landed (`--check` re-runs the same verification
   against the committed schema; it assumes the owner is still the only tenant, so run it
   right away):

   ```bash
   uv run --directory backend python migrations/apply.py 002
   uv run --directory backend python migrations/apply.py 002 --check
   ```

Reload the app: Today should render and the first-run goal wizard should open. From here on,
any address may sign up and gets a demo account (8.5).

### 8.4 Deploy

1. `vercel link` to a new project of your own. `vercel.json` deploys both services; the
   backend is one function with `maxDuration` 120.
2. Set every variable from 8.1 in the project's **Production** environment, plus
   `APP_ORIGIN=https://<your domain>` (pins the Origin check on state-changing requests;
   unset, `backend/auth.py` derives the allowed origin from Vercel's own URL variables —
   see `_allowed_origins`), `LANGFUSE_TRACING_ENVIRONMENT=production` and `ALLOWED_EMAIL`
   (the backend's owner check; one project's env serves both services). Do the same for
   **Preview** with
   `LANGFUSE_TRACING_ENVIRONMENT=preview` if you use previews.
3. `vercel deploy --yes` for a preview; promote it, or connect the GitHub repo so pushes to
   `main` deploy production (that is how the owner's project works — §5 rule 7).
4. Sign in on the deployed origin. If Neon Auth refuses the deployed domain, look for a
   trusted-domains list in the branch's Auth settings — local development does not need it,
   and this step has not been verified from a fresh project.

### 8.5 What a second account gets

Any verified account can sign in. Your `ALLOWED_EMAIL` is the owner: unlimited. Everyone
else is a demo account — one log per UTC day across every lane and ten paid re-resolutions,
answered with a 429 carrying the reset time past that (DESIGN §5). Every account's rows are
isolated by the database, not by the app code: a demo user's aliases, saved meals, logs,
entries and settings are theirs alone; the `foods` cache is shared, because it holds public
nutrition data and nothing personal. A demo account starts with no `settings` rows — the
readers fall back to the defaults and the goal wizard writes its own (BACKLOG 99) — and
nothing expires its rows (102). A demo's tier-3 agent searches FDC only — no web tools, so a
chain-restaurant item comes back `not_found` rather than transcribed — and the shared `foods`
cache takes a demo's rows under the adapter's own names (DESIGN §8.9). The one-a-day rule is the
database's: a unique index on the attempt ledger admits the day's attempt, so two concurrent
tries cannot double it.
