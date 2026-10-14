"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import "@/app/history/history.css";
import {
  fetchHistory,
  HistoryUnavailable,
  isoDate,
  monthShort,
  parseISO,
  type HistoryRange,
  type RangeKind,
} from "./HistoryApi";
import ErrorNotice from "./ErrorNotice";
import HistoryView from "./HistoryView";
import Skeleton from "./Skeleton";
import { RESUME_STALE_MS, useOnForeground } from "./useOnForeground";

const KINDS: { kind: RangeKind; label: string; title: string }[] = [
  { kind: "3d", label: "3D", title: "3 days" },
  { kind: "w", label: "W", title: "Week" },
  { kind: "m", label: "M", title: "Month" },
  { kind: "y", label: "Y", title: "Year" },
];

function spanLabel(range: HistoryRange): string {
  if (!range.start || !range.end) return "";
  const a = parseISO(range.start);
  const b = parseISO(range.end);
  if (range.range === "y") {
    return `${monthShort(a)} ${a.getFullYear()} – ${monthShort(b)} ${b.getFullYear()}`;
  }
  return a.getMonth() === b.getMonth()
    ? `${monthShort(a)} ${a.getDate()} – ${b.getDate()}`
    : `${monthShort(a)} ${a.getDate()} – ${monthShort(b)} ${b.getDate()}`;
}

/** §7.4 History: segmented 3D · W · M · Y (every range ends today, per
 *  routes/history.py), streak chip up top, tap a day → /history/<date>.
 *  The tab bar mounts from app/history/layout.tsx. */
export default function History({ initialKind = "3d" }: { initialKind?: RangeKind }) {
  const router = useRouter();
  const [today, setToday] = useState(() => isoDate(new Date()));
  const [kind, setKind] = useState<RangeKind>(initialKind);
  // The ranges read this visit, by kind, for the day they end on: a segment
  // tap shows its range at once and re-reads it underneath. A new local day
  // leaves them behind — every range ends today.
  const [ranges, setRanges] = useState<{
    day: string;
    byKind: Partial<Record<RangeKind, HistoryRange>>;
  }>({ day: today, byKind: {} });
  // one number for every range: the last one read stays up across a switch
  const [streak, setStreak] = useState<number | null>(null);
  const [error, setError] = useState<"unavailable" | "failed" | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry.
  const [attempt, setAttempt] = useState(0);

  // Installed, there is no reload and no pull-to-refresh (useOnForeground):
  // a resume on a new local date moves every range to end on the new today,
  // so the card labelled Today is never yesterday's; a long absence re-reads.
  useOnForeground((away) => {
    const d = isoDate(new Date());
    if (d !== today) setToday(d);
    else if (away >= RESUME_STALE_MS) setAttempt((n) => n + 1);
  });

  useEffect(() => {
    let cancelled = false;
    setError(null);
    fetchHistory(kind, today)
      .then((r) => {
        if (cancelled) return;
        setRanges((cur) => ({
          day: today,
          byKind: { ...(cur.day === today ? cur.byKind : {}), [kind]: r },
        }));
        setStreak(r.streak);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof HistoryUnavailable ? "unavailable" : "failed");
      });
    return () => {
      cancelled = true;
    };
  }, [kind, today, attempt]);

  const range = ranges.day === today ? ranges.byKind[kind] : undefined;

  return (
    <main className="shell history">
      <header className="brand">
        <h1>History</h1>
        {/* no chip at 0: "no streak" on day one only labels the user */}
        {streak != null && streak > 0 ? (
          <span
            className="streak-chip live"
            title="Consecutive days with at least one entry"
          >
            {streak}-day streak
          </span>
        ) : null}
      </header>

      <div className="segmented" role="tablist" aria-label="Range">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="tab"
            title={k.title}
            aria-selected={kind === k.kind}
            className={kind === k.kind ? "active" : ""}
            onClick={() => setKind(k.kind)}
          >
            {k.label}
          </button>
        ))}
      </div>

      {/* always in place, so the content under it never moves by a line */}
      <p className="range-label">{range ? spanLabel(range) : "\u00a0"}</p>

      {error ? (
        <ErrorNotice
          message={
            error === "unavailable"
              ? "The year view needs GET /api/history — not wired yet."
              : range
                ? "Couldn't refresh this range."
                : "Couldn't load this range."
          }
          onRetry={() => setAttempt((n) => n + 1)}
        />
      ) : null}
      {range || !error ? (
        <div>
          {range ? (
            <HistoryView
              kind={range.range}
              range={range}
              today={today}
              onDay={(date) => router.push(`/history/${date}`)}
            />
          ) : (
            // not read yet: that range's shape, never the last range dimmed
            <div className={`${kind === "3d" ? "hcards" : "hcharts"} hist-loading`}>
              <Skeleton rows={kind === "3d" ? 3 : 2} height={180} radius={20} />
            </div>
          )}
        </div>
      ) : null}
    </main>
  );
}
