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
import { peek, put } from "./readCache";
import { useScrollMemory } from "./useScrollMemory";
import Skeleton from "./Skeleton";
import { RESUME_STALE_MS, useOnForeground } from "./useOnForeground";
import { OFFLINE_NOTE, useOnline } from "./useOnline";

// §7.4's words, not letters: each segment is ~70px even at 320, and the
// word is also the tab's accessible name (a title was only a description)
const KINDS: { kind: RangeKind; label: string }[] = [
  { kind: "3d", label: "3 days" },
  { kind: "w", label: "Week" },
  { kind: "m", label: "Month" },
  { kind: "y", label: "Year" },
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

/* Each range's card or chart height at 390px, so its blocks hold the
   place the data lands in. */
const LOADING_HEIGHT: Record<RangeKind, number> = { "3d": 207, w: 215, m: 259, y: 259 };

/* The range last picked, kept for as long as the document lasts: back from
   a day, or a return to the tab, finds History on it, as native tabs do. A
   hard load is "3d" on server and client alike, so hydration is unaffected;
   only a reload resets it. */
let lastKind: RangeKind | null = null;

const rangeKey = (kind: RangeKind, today: string) => `history:${kind}:${today}`;

/** §7.4 History: segmented 3 days · Week · Month · Year (every range ends
 *  today, per routes/history.py), the streak up top, tap a day →
 *  /history/<date>.
 *  The tab bar mounts from app/history/layout.tsx. */
export default function History({ initialKind = "3d" }: { initialKind?: RangeKind }) {
  const router = useRouter();
  const [today, setToday] = useState(() => isoDate(new Date()));
  const [kind, setKindState] = useState<RangeKind>(() => lastKind ?? initialKind);
  const setKind = (k: RangeKind) => {
    lastKind = k;
    setKindState(k);
  };
  // The ranges read, by kind, for the day they end on: a segment tap shows
  // its range at once and re-reads it underneath. A new local day leaves
  // them behind — every range ends today. Seeded from readCache, so a return
  // to the tab paints what this visit already read.
  const [ranges, setRanges] = useState<{
    day: string;
    byKind: Partial<Record<RangeKind, HistoryRange>>;
  }>(() => ({
    day: today,
    byKind: Object.fromEntries(
      KINDS.flatMap(({ kind: k }) => {
        const r = peek<HistoryRange>(rangeKey(k, today));
        return r ? [[k, r]] : [];
      }),
    ),
  }));
  // one number for every range: the last one read stays up across a switch
  const [streak, setStreak] = useState<number | null>(
    () => ranges.byKind[kind]?.streak ?? null,
  );
  const [error, setError] = useState<"unavailable" | "failed" | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry.
  const [attempt, setAttempt] = useState(0);
  const online = useOnline();

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
        put(rangeKey(kind, today), r);
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
  useScrollMemory(range != null);
  const kindLabel = KINDS.find((k) => k.kind === kind)?.label;

  return (
    <main className="shell history">
      <header className="brand">
        <h1>History</h1>
        {/* a label, not a control; at zero there is nothing to say */}
        {streak ? (
          <span
            className="streak-chip"
            title="Consecutive days with at least one entry"
          >
            <span className="num">{streak}</span>-day streak
          </span>
        ) : null}
      </header>

      {/* mounted online too, empty and out of the flow ([role=status]:empty),
          so the note is announced as it arrives, as on Today */}
      <div
        className={online ? undefined : "notice warn offline-note"}
        role="status"
      >
        {online ? null : <span>{OFFLINE_NOTE}</span>}
      </div>

      <div className="m3-seg" role="tablist" aria-label="Range">
        {KINDS.map((k) => (
          <button
            key={k.kind}
            type="button"
            role="tab"
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

      {/* the span label stays outside: from 900px it sits beside the tabs.
          The panel is a stable wrapper, so a switch doesn't replay the
          page's rise. */}
      <div role="tabpanel" aria-label={kindLabel}>
        {error ? (
          <ErrorNotice
            message={
              error === "unavailable"
                ? "The year view isn't available right now."
                : range
                  ? "Couldn't refresh this range."
                  : "Couldn't load this range."
            }
            onRetry={() => setAttempt((n) => n + 1)}
          />
        ) : null}
        {range ? (
          <HistoryView
            kind={range.range}
            range={range}
            today={today}
            onDay={(date) => router.push(`/history/${date}`)}
          />
        ) : error ? null : (
          // not read yet: that range's shape, never the last range dimmed
          <div className={`${kind === "3d" ? "hcards" : "hcharts"} hist-loading`}>
            <Skeleton rows={kind === "3d" ? 3 : 2} height={LOADING_HEIGHT[kind]} radius={20} />
          </div>
        )}
      </div>
    </main>
  );
}
