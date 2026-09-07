# Tally

Tally is a nutrition logger you talk to. Say or type what you ate, check the pre-filled
list, tap once, and it is in your diary with real database numbers.

[Screenshot: none committed yet — `frontend/public/` holds only the app icons.]

## The idea

Most food apps make you search, scroll, pick a serving and set a meal for every item, every
time. Tally does that work up front and then remembers. "Two eggs and a fairlife" becomes two
rows with the foods, portions and meal already filled in, taken from USDA FoodData Central
records — whole foods, branded products, barcodes — never from a model's guess. Every commit
teaches it: the phrases you use, the foods they mean, the portions you usually take. A meal
you have logged before resolves from your own history with zero AI calls, in one database
round-trip. Only a food you have never logged touches the network, once, and then it is
learned.

The one place a guess is allowed is a photo. Vision identifies what is on the plate and
estimates the amounts; the foods still resolve against the database, and every estimated
portion is flagged in the list and in the diary and has to be confirmed before it commits.
Text and voice never estimate — if a food cannot be found, the app says so.

## How it works

One repo, one Vercel project, two Vercel Services on one origin: `frontend/` is a Next.js
PWA, `backend/` is a FastAPI app, and `vercel.json` sends `/api/*` to the backend and
everything else to the frontend. No CORS; cookie auth works unchanged. Neon Postgres holds
the diary, the learned aliases and every agent transcript, and Neon's managed auth keeps
users and sessions in the same database. Each account's rows are separated by Row-Level
Security in Postgres, not by application code — the app runs every transaction as a database
role that cannot bypass the policies; the food cache is the one shared table.

Resolving an item is a cascade, cheapest first. Before any model call, the whole utterance
is checked against your saved meals and your aliases; a hit stages in one query. Otherwise
a small model call splits the sentence into items and each one goes through the tiers.
Tier 0: a saved meal ("my usual breakfast") expands to its stored foods and portions.
Tier 1: an exact phrase you have used before maps to its food, with alternates shown as
chips when the same phrase has meant two things. Tier 2: a phrasing you have not used before
but a food you have — trigram similarity over your aliases plus a semantic hint from the
parse. Tier 3: a food you have never logged — an agent searches FDC and, for chain
restaurants, the chain's own published nutrition page, and picks a database record. The
agent never outputs nutrition numbers; it only selects ids. Commit snapshots the macros,
writes the entries, and learns the alias and portion for next time.

## Stack

- Next.js 16 (App Router, TypeScript), Serwist for the PWA
- FastAPI, Python 3.12, `uv`
- Neon Postgres (`pg_trgm`) with Neon Managed Better Auth; Row-Level Security for tenancy
- OpenAI Agents SDK — `gpt-5.6-luna` parses utterances and reads photos, `gpt-5.6-terra` runs the food-search agent
- Deepgram Nova-3 for speech, OpenAI transcribe as the fallback
- USDA FoodData Central for all food data; Open Food Facts as the quality-gated barcode fallback; chain restaurants via cited transcription of the chain's own page
- FireTrace (self-hosted) for traces and commit-time scores; Langfuse only for the correction → replay eval harness, until FireTrace has datasets
- API types defined once in pydantic and generated to TypeScript from the OpenAPI schema

## Try it

The live app is at **https://tally.art3m1s.me**. Sign up with an email and password and you
get a demo account: one log per day, in any lane — voice, text, photo, barcode or search —
resetting at midnight UTC, plus ten re-resolutions a day for the calls that re-run a model
on an edited row. Your rows are your own; the database isolates every account. A demo is for
trying the loop, not for keeping a diary. Fork it for that.

## Fork it and run your own

Everything here is yours to take: MIT, no server of mine involved, and the whole thing is
built to be run by one person on free tiers. You need four accounts (Vercel, Neon, OpenAI,
Deepgram) and a free USDA key. Only OpenAI meters usage — for one person logging a few meals
a day it is roughly a dollar or two a month, and most logs cost nothing at all because a
repeat meal never reaches a model.

```bash
gh repo fork IdkwhatImD0ing/nutritiontracker --clone && cd nutritiontracker
cp .env.example .env          # fill in your own keys
cd backend && uv sync && cd ../frontend && pnpm install
```

Then create a Neon project with auth enabled, run the two migrations, point Vercel at your
fork, and set `ALLOWED_EMAIL` to your address so your account is the owner and everyone
else is a demo. **[docs/ONBOARDING.md §8](docs/ONBOARDING.md)** walks each step with the
exact commands and the two ordering rules that will otherwise bite you.

### Or hand it to a coding agent

This project was built by agents, so it is written to be legible to one. Point an agent at
your fork and paste this:

```text
Set up this repository as my own running instance.

Read docs/ONBOARDING.md first — section 8 is the from-scratch path — then docs/DESIGN.md
for the architecture. Work through the setup with me:

1. Tell me exactly which accounts and API keys I need to create, with the URL for each and
   what the free tier gives me. Wait for me to paste each value into .env myself — never
   ask me to paste a secret into the chat, and never print one back.
2. Once .env is filled in, verify each key actually works before going further, and tell
   me which ones failed rather than assuming.
3. Apply migrations 001 then 002 with backend/migrations/apply.py, using --dry-run first
   and --check after. Set ALLOWED_EMAIL to my address; that account becomes the owner and
   every other sign-up is a demo capped at one log a day.
4. Get it running locally (two servers), and confirm the auth round-trip works — not just
   /api/health. `curl /api/auth/token` must answer Better Auth's 401, not FastAPI's 404.
5. Deploy to Vercel as two Services on one origin per vercel.json, set APP_ORIGIN to my
   domain, and add that domain to Neon Auth's trusted list.

Constraints that are not style preferences — they are in CLAUDE.md and each one has already
caused a bug: never commit .env or print a secret value; API types are generated from
pydantic, so re-run `pnpm run typegen` after changing a model and never hand-edit
lib/api-types.ts; DB tests run inside force-rollback transactions and must leave no rows;
pin full model ids, never a bare alias.

Ask me before anything irreversible: applying a migration, deploying, or pushing.
```

## How it was built

Tally was built in phases — a scaffold, then milestones M0 through M4 — by orchestrated coding
agents, against a design document that was written and adversarially reviewed before any code
existed. Each phase ran the same way: parallel build agents on disjoint files, an integrator,
then a review panel — bugs, security, and design conformance checked clause by clause against
the spec — and every finding was verified in code before it was fixed; by the later phases
that meant reproducing it against the live database inside a rolled-back transaction. The
human steps were the phase gates, the keys, and the phone.
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) is the plan; [docs/BACKLOG.md](docs/BACKLOG.md)
is the ledger of every finding and what happened to it.

## Docs

- [docs/ONBOARDING.md](docs/ONBOARDING.md) — start here: setup, commands, layout, current state, running your own instance
- [docs/DESIGN.md](docs/DESIGN.md) — the system design, and the authoritative spec
- [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) — the phased, agent-orchestrated build plan
- [docs/BACKLOG.md](docs/BACKLOG.md) — the findings ledger
- [docs/BRIEF.md](docs/BRIEF.md) — the original requirements

## License

[MIT](LICENSE) © 2026 Bill Zhang. Use it, fork it, run your own.

The nutrition data is not mine to license: USDA FoodData Central is public
domain, and chain-restaurant values are transcribed from each chain's own
published page and stored with a link back to it.
