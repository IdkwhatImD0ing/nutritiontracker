# CLAUDE.md

Guidance for Claude Code in this repo. Keep it short — every line here competes for attention with the actual task. If a line wouldn't change Claude's behavior, delete it.

## Working principles

**Think before coding.** State assumptions out loud. If a request is ambiguous, ask or present the interpretations — don't silently pick one. Suggest a simpler approach when you see one, and push back when warranted.

**Simplicity first.** Write the minimum code that solves the stated problem. No speculative features, no unrequested flexibility, no error handling for impossible cases. If it's bloated, rewrite it smaller. Test: would a senior engineer call this overcomplicated?

**Surgical changes.** Edit only what the request requires. Preserve existing style and formatting. Don't refactor working code. Only remove imports/variables/functions that *your* change orphaned. Flag pre-existing dead code — don't delete it. Core test: every changed line traces directly to the request.

**Goal-driven execution.** Define verifiable success criteria before starting. Turn vague tasks into testable outcomes. For multi-step work, outline the plan with verification checkpoints. "Make it work" is not a success criterion.

## Honesty rules (read every turn)

- Before claiming a function, class, type, or import exists, verify it — read the file or grep for it. Never fabricate symbols.
- If you can't verify something, say "I haven't verified this" explicitly, and don't write code that depends on the unverified claim.
- Don't add a library that isn't already referenced in this project without asking first.
- Don't claim a test or build passed unless you actually ran the command this session.
- Never invent error messages, API responses, or stack traces. If you didn't see it, say so.
- When you genuinely don't know, "I don't know" or "I need to check first" is the correct answer — always better than a confident guess.

## Verification protocol

Before writing or editing code that uses a symbol, do one of:
1. Read the file where it's defined and confirm the signature.
2. `grep -r "symbolName" .` (or Glob) to find it.
3. Check `package.json` / `requirements.txt` / `Cargo.toml` / equivalent for the dependency.

If you skip verification, prefix the code with `// UNVERIFIED: I have not confirmed this symbol exists`.

Prefer plan-then-execute mode for any task touching more than one file — enter plan mode (Shift+Tab) before starting.

Invoke the `fact-checker` subagent (`.claude/agents/fact-checker.md`) before commits and before user-facing summaries.

## Project context

**Tally** — single-user voice-first nutrition logger. Next.js PWA + Python FastAPI deployed as
two Vercel Services on one origin; Neon Postgres; OpenAI Agents SDK; Deepgram STT; FireTrace traces.

**New here? Read [docs/ONBOARDING.md](docs/ONBOARDING.md) first** — setup, commands, layout, current state.
[docs/DESIGN.md](docs/DESIGN.md) is the authoritative spec (§3 resolver, §4 schema, §7 UI, §8 decisions
with the *why*). [docs/BACKLOG.md](docs/BACKLOG.md) is the findings ledger.

Non-obvious facts, each of which has already caused a bug:

- API types are defined **once** in pydantic and generated to TS. Change a model → re-run
  `cd frontend && pnpm run typegen`. Never hand-edit `frontend/lib/api-types.ts`.
- Pin full model ids (`gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`, `gpt-6-astra`) —
  never a bare family alias (`gpt-5.6` routes to Sol at flagship prices).
- DB tests hit the **production** Neon database inside force-rollback transactions (no dev
  branch yet, BACKLOG 18). Keep that pattern; leave no rows behind. `-m live` spends real money.
- Tier-0 (saved-meal) rows never learn aliases or portion defaults — the saved meal is the memory.
- Every rewrite of `item_resolutions.staged` must append `KEEP_LOG_KEYS_SQL`, or the diary-date
  shift is silently lost (BACKLOG 57).
- Estimation is gated off outside the photo lane; offline is a deliberate non-feature.

## Guardrails

- Never commit secrets, `.env` files, or credentials. Never print a secret value — names only.
- Don't commit or push unless asked. **Pushing `main` deploys production** — the Vercel
  project auto-deploys from GitHub (`vercel project inspect` doesn't show it; the deployment
  metadata does). So "push" means "ship"; confirm that is what was asked.
- Agents deploy previews only (`vercel deploy --yes`). Production changes only via a push
  the human asked for.
- **Commit and push only outside working hours** (Mon–Fri 09:00–17:00 America/Los_Angeles —
  the owner's rule, 2026-09-03; the day ends at 5pm, 2026-09-08). A local hook enforces it;
  inside the window, finish the work, run the suites, deploy a preview, and leave the
  commit for after 17:00.
