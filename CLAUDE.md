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

<!-- TODO: fill in once code lands — what this is, stack, non-obvious commands, architecture facts not visible in any single file. -->

## Guardrails

- Never commit secrets, `.env` files, or credentials.
- Don't commit or push unless asked.
