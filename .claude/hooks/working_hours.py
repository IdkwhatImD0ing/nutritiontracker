# -*- coding: utf-8 -*-
"""PreToolUse hook: refuse `git commit` / `git push` during working hours.

Bill's rule (2026-09-03): commit and push only outside working hours, because
pushing `main` auto-deploys production. Working hours are assumed to be
Monday-Friday 09:00-18:00 America/Los_Angeles; weekends are open.

Reads the hook's JSON on stdin ({tool_name, tool_input: {command}}), prints a
deny decision when a git commit/push would run inside the window, and prints
nothing (allow) otherwise. Never blocks anything that is not a commit or push.

Test without waiting for the clock: set WORKING_HOURS_NOW to an ISO datetime,
e.g. WORKING_HOURS_NOW=2026-09-06T10:00 (a Saturday).
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
START_HOUR, END_HOUR = 9, 18  # 09:00 inclusive .. 18:00 exclusive
GIT_WRITE = re.compile(r"\bgit\b(?:\s+\S+)*?\s+(commit|push)\b")


def now() -> datetime:
    override = os.environ.get("WORKING_HOURS_NOW")
    if override:
        return datetime.fromisoformat(override).replace(tzinfo=TZ)
    return datetime.now(TZ)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # unreadable input: never block on our own bug
    command = str((payload.get("tool_input") or {}).get("command") or "")
    m = GIT_WRITE.search(command)
    if not m:
        return 0
    t = now()
    working = t.weekday() < 5 and START_HOUR <= t.hour < END_HOUR
    if not working:
        return 0
    verb = m.group(1)
    reopen = t.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    reason = (
        f"Working-hours rule: no git {verb} Mon-Fri 09:00-18:00 America/Los_Angeles "
        f"(it is {t.strftime('%a %H:%M')} PT). Pushing main deploys production. "
        f"Deploy a preview and try again after {reopen.strftime('%H:%M')} PT, "
        f"or the owner can say 'push anyway'."
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
