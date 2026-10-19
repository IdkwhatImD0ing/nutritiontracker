"use client";

import { useState } from "react";
import type { DemoQuota } from "./MeApi";
import { SheetClose } from "./SheetHeader";

/** Two dismissals, because the strip says two different things: the quota
 *  ("fresh") and, once it is spent, when the next log opens ("spent"). A
 *  strip dismissed while fresh comes back once, spent. */
const DISMISS_KEY = "tally.demo-banner.v1";

function readDismissed(): string | null {
  try {
    return sessionStorage.getItem(DISMISS_KEY);
  } catch {
    return null;
  }
}

/** "at 4:00 PM", or "tomorrow at 4:00 PM" when the next UTC midnight lands
 *  on a different local date than now. The + sheet's spent line uses it too.
 *  The time's spaces are no-break, so "PM" never starts a line alone. */
export function nextLogAt(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "tomorrow";
  const time = at
    .toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    .replace(/\s/g, "\u00a0");
  return at.toDateString() === new Date().toDateString()
    ? `at ${time}`
    : `tomorrow at ${time}`;
}

type Props = { demo: DemoQuota };

/** One-line strip under the Today header for non-owner accounts: the quota,
 *  and once it is spent, when the next log opens (local time). Dismissible
 *  per session. Today mounts it only after GET /api/me answers, so the
 *  sessionStorage read in the initializer runs on the client alone (no
 *  hydration mismatch). Reuses .pending-pill; .demo-banner is a hook for
 *  the design pass, which owns the CSS. */
export default function DemoBanner({ demo }: Props) {
  const [dismissed, setDismissed] = useState(readDismissed);
  const spent = demo.used_today >= demo.limit;
  const phase = spent ? "spent" : "fresh";
  if (dismissed === "spent" || dismissed === phase) return null;

  const dismiss = () => {
    setDismissed(phase);
    try {
      sessionStorage.setItem(DISMISS_KEY, phase);
    } catch {
      /* storage unavailable — dismissed for this render tree only */
    }
  };

  return (
    <div className="pending-pill demo-banner" role="note">
      <span style={{ flex: 1 }}>
        Demo account — {demo.limit} {demo.limit === 1 ? "log" : "logs"} per day
        {spent ? ` · next log ${nextLogAt(demo.resets_at)}` : ""}
      </span>
      <SheetClose label="Dismiss" onClick={dismiss} />
    </div>
  );
}
