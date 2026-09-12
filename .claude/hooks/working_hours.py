# -*- coding: utf-8 -*-
"""Refuse `git commit` / `git push` during working hours, unless the owner
has said so this turn.

Bill's rule (2026-09-03): commit and push only outside working hours, because
pushing `main` auto-deploys production. Working hours are Monday-Friday
09:00-17:00 America/Los_Angeles (the end hour was 18:00 until Bill corrected
it to 17:00 on 2026-09-08); weekends are open.

One script, two hook events (registered in .claude/settings.local.json):

- PreToolUse (Bash|PowerShell): reads {session_id, tool_input: {command}},
  prints a deny decision when a git commit/push would run inside the window
  with no override, and prints nothing (allow) otherwise. Never blocks
  anything that is not a commit or push.
- UserPromptSubmit (2026-09-11): the owner's override. A prompt that says
  "push anyway" or "commit anyway" (not negated in its clause, asked as a
  question, or quoted) allows commit/push for the rest of that turn; the
  next prompt without it ends the override, and it lapses after an hour
  regardless. Only a prompt the owner typed counts: a subagent's prompt
  (it carries agent_id) is the agent's words and never sets it.

Test without waiting for the clock: set WORKING_HOURS_NOW to an ISO datetime,
e.g. WORKING_HOURS_NOW=2026-09-06T10:00 (a Saturday).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
START_HOUR, END_HOUR = 9, 17  # 09:00 inclusive .. 17:00 exclusive
GIT_WRITE = re.compile(r"\bgit\b(?:\s+\S+)*?\s+(commit|push)\b")
OVERRIDE = re.compile(r"(?<!['\"‘“])\b(?:push|commit) anyway\b", re.I)  # not in quotes
NEGATION = re.compile(r"\b(?:not|never|dont|cannot)\b|n['’]t\b", re.I)
OVERRIDE_TTL = timedelta(hours=1)
STATE_DIR = Path(tempfile.gettempdir()) / "claude-working-hours"


def now() -> datetime:
    override = os.environ.get("WORKING_HOURS_NOW")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=TZ)
    return datetime.now(TZ)


def state_file(payload: dict) -> Path | None:
    """One file per session: when the owner said "push anyway"."""
    session = str(payload.get("session_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session):
        return None
    return STATE_DIR / session


def says_override(text: str) -> bool:
    """"push anyway" said outright: not inside a question, and with no
    negation before it in its clause ("don't just push anyway" is a no;
    "I know it's not 5 yet, push anyway" is a yes). Doubt means no."""
    for sentence in re.split(r"(?<=[.!?\n])", text):
        if sentence.rstrip().endswith("?"):
            continue
        for clause in re.split(r"[,;:]", sentence):
            m = OVERRIDE.search(clause)
            if m and not NEGATION.search(clause[: m.start()]):
                return True
    return False


def on_prompt(payload: dict) -> None:
    """UserPromptSubmit: set the override on "push anyway", clear it otherwise."""
    if payload.get("agent_id") or payload.get("agent_type"):
        return
    path = state_file(payload)
    if path is None:
        return
    prompt = payload.get("prompt")
    text = prompt.get("text") if isinstance(prompt, dict) else prompt
    if says_override(str(text or "")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(now().isoformat(), encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


def overridden(payload: dict, t: datetime) -> bool:
    path = state_file(payload)
    if path is None:
        return False
    try:
        said = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return timedelta(0) <= t - said < OVERRIDE_TTL


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unreadable input: never block on our own bug
    if payload.get("hook_event_name") == "UserPromptSubmit":
        try:
            on_prompt(payload)
        except Exception:
            pass  # never block a prompt on our own bug
        return 0
    command = str((payload.get("tool_input") or {}).get("command") or "")
    m = GIT_WRITE.search(command)
    if not m:
        return 0
    t = now()
    working = t.weekday() < 5 and START_HOUR <= t.hour < END_HOUR
    if not working or overridden(payload, t):
        return 0
    verb = m.group(1)
    reopen = t.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    reason = (
        f"Working-hours rule: no git {verb} Mon-Fri {START_HOUR:02d}:00-{END_HOUR:02d}:00 "
        f"America/Los_Angeles "
        f"(it is {t.strftime('%a %H:%M')} PT). Pushing main deploys production. "
        f"Deploy a preview and try again after {reopen.strftime('%H:%M')} PT, "
        f"or the owner can type 'push anyway' in a message, which allows it for that turn."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
