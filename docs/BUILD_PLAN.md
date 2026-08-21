# Usual — Agent-Orchestrated Build Plan

**Companion to [DESIGN.md](DESIGN.md)** (the spec every agent builds against and every reviewer audits against). 2026-08-20.

## Operating principles

1. **Contracts freeze first, then everything fans out.** The pydantic models + DB schema + OpenAPI-generated TS types are built by one agent in Phase 0 and then *frozen*: parallel agents never touch them; contract changes go through a single dedicated agent + regeneration, so parallel branches can't drift.
2. **Every phase has the same shape:** parallel build agents (git-worktree isolation, one workstream each) → one integrator agent (wires seams, runs the end-to-end test) → an adversarial review panel (correctness / security / **design-conformance vs DESIGN.md §refs**) → findings fixed → deploy → **Bill gate** (poke the deployed URL, approve the next phase).
3. **The design-conformance reviewer is the distinctive move:** DESIGN.md is specific enough (§3's algorithms, §4's invariants, §7.1's row contract) that an agent can diff code against it clause by clause — the same adversarial pattern that hardened the design now guards the build.
4. **Live-API tests are first-class** (all 11 keys are verified working): each adapter ships with tagged live tests, rate-limit-aware, runnable in CI against the Neon dev branch + Langfuse `development` environment.
5. **One workflow per phase** (≤15 agents each), sequential phases — Bill stays in the loop between them.

## Phases

| Phase | Parallel build agents | Integration test (the gate) |
|---|---|---|
| **0 — Scaffold & contracts** *(mostly sequential, ~3 agents)* | ① Monorepo scaffold: `frontend/` Next PWA shell + `backend/` FastAPI hello + `vercel.json` Services + **deploy spike** (settles the Services-beta unknown on day one, fallback ready) · ② Schema migration SQL (§4, all tables + seeds incl. baseline=0) applied to a Neon dev branch · ③ Pydantic contracts → OpenAPI → `openapi-typescript` gen + CI no-diff check | Hello-world deployed on one origin; schema live; type-gen green |
| **1 — M0: the loop** *(~7 build + 3 review)* | A: FDC adapter (generic+branded, `gtinUpc` ladder incl. the 10-pantry-barcode spike, GS1 units, 404 tolerance) · B: resolver core (`norm()`, short-circuit, tier 1, §3.5 idempotent commit tx, learning writes) · C: decompose service (Luna `responses.parse`, `buildAgentContext`, cache layout) · D: tier-3 agent (`openai-agents` + tools + client-kick route + watchdog) with Langfuse auto-instrumentation · E: frontend Today + staging sheet + commit (from §7.4 mockups) + Neon Auth wiring · F: instrumentation plumbing (`logs`/`item_resolutions`, trace ids, commit-time scores) | Type "two eggs and a fairlife" on the deployed app → staged → commit → diary; second identical dinner short-circuits <1s; **Bill logs a real dinner** |
| **2 — M1: friction killers** *(~6 build + 3 review)* | Per-feature agents: meal header chip + inference · portion sheet (stepper + serving picker) · disambiguation chips + explicit-choice learning · tier 2 (trigram + inlined index) · cooked/raw code enforcement · add/remove rows + entry PATCH + `commit-item` | A repeat meal commits with zero edits; **a correction changes the very next proposal** |
| **3 — M2: voice + PWA** *(~5 build + 2 review + 1 human step)* | Deepgram `Transcriber` + keyterm feed from aliases · MediaRecorder capture + blob/transcript validation + text fallback · manifest + Serwist install · web-search **citation-capture verification** (the flagged watch-item) · capture-failure telemetry | **Human step: the on-device iPhone spike** (install → record → close → reopen → record) — the one thing agents can't do; voice path chosen by its outcome |
| **4 — M3: meals, dashboard, experiment** *(~6 build + 2 review)* | Saved meals + tier 0 + whole-utterance triggers · Library (aliases, pins, food detail) · History with 3D/W/M/Y ranges · goal wizard first-run · `/experiment` charts · **eval harness** (Langfuse datasets: flagged production traces → replay — same Python agent module as production) | "My usual breakfast" stages instantly; the alias-hit-rate curve renders and rises |
| **5 — M4: photo** *(~3 build + 2 review)* | Camera/upload capture adapter · GPT-5.6 vision decompose (items + portion estimates, §3.4a flagging) · resolver integration | A photographed plate stages with resolved staples and flagged estimates |

## Review panel (every phase, adversarial)

- **Bugs:** concrete failure scenarios only, verified before reported (the Rev-2 critique pattern, applied to code).
- **Security:** auth dependency on every route, no secret leakage, Origin checks, JWT verification correctness.
- **Design conformance:** code vs DESIGN.md clause-by-clause — tier rules §3, invariants §4, row contract §7.1, estimation gate §3.4a.
- Findings fixed by the original workstream's agent (context intact via SendMessage), re-verified, then deploy.

## What stays human

Bill: phase gates (poke each deploy), the iPhone spike (Phase 3), pantry barcodes for the Phase-1 spike, YC-deal redemptions, and any contract change approval. Everything else — code, tests, integration, review, docs — is agent work.
