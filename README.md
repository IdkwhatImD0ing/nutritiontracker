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
- USDA FoodData Central for all food data; Open Food Facts is the designed quality-gated fallback (adapter not wired yet); chain restaurants via cited transcription of the chain's own page
- Langfuse for traces, commit-time scores and the correction → replay eval loop
- API types defined once in pydantic and generated to TypeScript from the OpenAPI schema

## Try it

The live app is at https://nutritiontracker-tau.vercel.app. Sign up with an email and password and you
get a demo account: one log per day, in any lane — voice, text, photo, barcode or search —
resetting at midnight UTC, plus ten re-resolutions a day for the calls that re-run a model
on an edited row. Your rows are your own; the database isolates every account. But a demo
is for trying the loop, not for keeping a diary. Run your own instance for that.

## Run it yourself

[docs/ONBOARDING.md](docs/ONBOARDING.md) has the full setup, and its "Running your own
instance" section is the from-scratch path: the keys you need (free tiers, except that
OpenAI meters usage — a dollar or two a month for one person), a Neon project with auth
enabled, the two migrations, and deploying the two services to Vercel. Once the env is in
place, local development is two commands.

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
