# Usual — Open Findings Backlog (post-Phase-1 review panel)

Verified-in-code before fixing is mandatory — reviewers can be wrong. Fixed items get struck through with the phase that fixed them. Sources: Phase 1 adversarial panel (bugs/security/conformance), 2026-08-20. The 6 most severe findings (3 blockers, 3 majors) were fixed in Phase 1's fix pass; these remain.

## Majors (target: Phase 2)

1. **Watchdog incomplete** (`routes/logs.py`): §3.4 stale→re-kick-once→not_found lifecycle only partially implemented — a failed tier-3 run can leave an item spinning if the client never re-polls. Complete the server-side flip + attempts accounting.
2. **`staged_ms` has no write path** (`routes/logs.py` + frontend): §5's `PATCH /api/logs/:id/timings` doesn't exist and the client never measures render time — one of §9's two primary latency series is dark.
3. **Manual search escape hatch missing** (`routes/foods.py` + frontend): §12 M0 scope included `GET /api/foods/search`; `not_found` rows have no manual path.
4. **`was_edited` clobbering** (`resolver/commit.py`): recomputed proposed-vs-final and written unconditionally — clobbers disambiguation/edit nuance (§3.5 step 5 / §9 edit-rate fidelity).
5. **`get_food` None-handling at callers** (`services/tier3.py`): adapter returns None on 404 per §3.4; callers don't all tolerate it.
6. **`/api/resolve` ignores `unit`** and reimplements a divergent tier-1 `pick()` (missing mode-of-last-10) — unify with `resolver/cascade.py`'s implementation (§3.2, §7.3).
7. ~~**Core-macros guard missing** (`resolver/commit.py`): a serving lacking kcal/protein/carb/fat can snapshot partial macros into the diary silently — guard + verify-me instead.~~ *fixed Phase 2* — guard in `resolver/commit.py` (W2); fix-pass completed the user-visible half: refusals now ride the commit response (`CommitResponse.errors`, `routes/commit.py`) and surface as needs-attention pills (`PendingItems`) instead of being silently discarded, and the tier-1/2 staging path (`cascade._stage_resolved`) stages a partial-core-macros serving as verify-me (macros=None, low confidence) instead of crashing with a pydantic ValidationError.
8. **Meal header chip reverts on searching rows** (`frontend/StagingSheet.tsx`): tier-3 kick/poll responses overwrite the locally-set meal.
9. **brandOwner ranking not applied** (`sources/fdc.py`): §3.4 requires ranking branded hits by brandOwner (fuzzy-noisy name search).

## Minors (Phase 2 where cheap, else Phase 3)

10. Idempotency race on concurrent same-id `POST /api/logs` (advisory lock or ON CONFLICT-first).
11. `DELETE /api/logs/:id` (discard, adherence-relevant) not implemented; sheet close just drops client state.
12. Commit-item never called from the client (M1 wiring).
13. ~~`state_question` logic diverges from §3.4a on the tier-1 path.~~ *fixed Phase 2* — tier-1/2 now follows §3.4a's letter (the entry's own parsed state suppresses the question when intent is unstated), and a cooked/raw toggle answer on a resolved row resolves locally in `routes/resolve.py` (matching state: zero network; differing state: ONE targeted `search_generic` for the counterpart, recorded as a non-switch so `rejected_count` never lands on the user's own alias) — never a `run_tier3` re-entry.
14. JWKS refetch has no cool-down (amplification); `source_id` unencoded in FDC URL path; Origin check fail-open until `APP_ORIGIN` env is set in Vercel (set it in Phase 2 env sync).
15. Auth residuals (documented, partially by design): tokens omitting the verified-email claim pass; sign-up of the allowlisted address stays open for first-run. Revisit after Bill's first real sign-in (check the live token's claim shape, then tighten).
16. Commit latency ~10s when scores post (per-score Langfuse flush) — batch to one flush (§11 budget <500ms).
17. `pytest.mark.live` marker unregistered; centralize sys.path shim in `tests/conftest.py`.
18. Tier-3 E2E wrote to the production DB (self-cleaned) — create the Neon dev branch (Console, Vercel-managed org) before Phase 3 testing; §10 environments intent.
19. Deployment protection: decide on a Protection Bypass for Automation token so agents can e2e the deployed preview (Phase 1 verified via share-link).
20. *(Phase 2 fix-pass, deferred)* Tier-3 path (`services/tier3.py _needs_state_question`) still asks the state question when the chosen entry's own state disambiguates and intent is unstated — diverges from §3.4a's letter the same way the tier-1 path did; cheap now that the toggle resolves locally, but left untouched this pass (only tier-1/2 was in the finding).
21. *(Phase 2 fix-pass, deferred)* A cooked/raw toggle that swaps to the counterpart entry stamps the row `tier=3` and re-derives the portion via `resolve_item_from_source` — telemetry nuance (a toggled tier-1 row reads as tier 3; serving delta counts as an edit at commit).
22. *(Phase 2 fix-pass, deferred)* Commit-refused rows surface as honest "needs attention" pills (`PendingItems`) but have no in-place fix UI — recovery is re-log or manual search; a tap-to-fix affordance (reopen the row in verify-me) is future polish.
