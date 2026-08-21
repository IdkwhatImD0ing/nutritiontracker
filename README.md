# Usual

Voice-first nutrition logging: speak or type what you ate, confirm a pre-filled
list, one tap to commit. The app learns your foods, portions, and phrasings so
it gets faster every week.

## Layout

- `frontend/` — Next.js (App Router, TypeScript) PWA
- `backend/` — Python FastAPI (`main:app`), managed with `uv`
- `vercel.json` — Vercel Services config: both apps deploy as one project on one origin (`/api/*` → backend, everything else → frontend)

## Docs

- [docs/DESIGN.md](docs/DESIGN.md) — the system design (the build reference)
- [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) — the phased, agent-orchestrated build plan
- [docs/BRIEF.md](docs/BRIEF.md) — the original requirements
