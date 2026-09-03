# Tally

Voice-first nutrition logging: speak or type what you ate, confirm a pre-filled
list, one tap to commit. The app learns your foods, portions, and phrasings so
it gets faster every week.

## Layout

- `frontend/` — Next.js (App Router, TypeScript) PWA
- `backend/` — Python FastAPI (`main:app`), managed with `uv`
- `vercel.json` — Vercel Services config: both apps deploy as one project on one origin (`/api/*` → backend, everything else → frontend)

## Getting started

```bash
vercel link && vercel env pull .env --environment=development --yes && cp .env frontend/.env.local
cd backend && uv sync && cd ../frontend && pnpm install
```

Then run the two servers and open http://localhost:3000 — full instructions, verification
commands and project conventions are in **[docs/ONBOARDING.md](docs/ONBOARDING.md)**.

## Docs

- [docs/ONBOARDING.md](docs/ONBOARDING.md) — **start here**: setup, commands, layout, current state
- [docs/DESIGN.md](docs/DESIGN.md) — the system design (the authoritative spec)
- [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) — the phased, agent-orchestrated build plan
- [docs/BACKLOG.md](docs/BACKLOG.md) — the living findings ledger
- [docs/BRIEF.md](docs/BRIEF.md) — the original requirements
