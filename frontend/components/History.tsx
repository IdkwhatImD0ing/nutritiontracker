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
import HistoryView from "./HistoryView";
import { peek, put } from "./readCache";
import { useScrollMemory } from "./useScrollMemory";

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
  const [today] = useState(() => isoDate(new Date()));
  const [kind, setKindState] = useState<RangeKind>(() => lastKind ?? initialKind);
  const setKind = (k: RangeKind) => {
    lastKind = k;
    setKindState(k);
  };
  const [range, setRange] = useState<HistoryRange | null>(() =>
    peek<HistoryRange>(rangeKey(kind, today)),
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchHistory(kind, today)
      .then((r) => {
        if (cancelled) return;
        setRange(r);
        put(rangeKey(kind, today), r);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setRange(null);
        setError(
          e instanceof HistoryUnavailable
            ? "The year view isn't available right now."
            : "Couldn't load this range.",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, today]);

  const streak = range?.streak ?? null;
  useScrollMemory(range !== null);
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

      {range && range.range === kind ? (
        <p className="range-label">{spanLabel(range)}</p>
      ) : null}

      {/* the span label stays outside: from 900px it sits beside the tabs */}
      <div role="tabpanel" aria-label={kindLabel}>
        {error ? <p className="hist-error">{error}</p> : null}
        {loading && !range ? (
          <p className="hhint">
            <span className="spinner" />
            Loading…
          </p>
        ) : null}
        {range ? (
          // dimmed only while it is another range's: a cached one being
          // re-read is this range already
          <div className={loading && range.range !== kind ? "hist-stale" : undefined}>
            <HistoryView
              kind={range.range}
              range={range}
              today={today}
              onDay={(date) => router.push(`/history/${date}`)}
            />
          </div>
        ) : null}
      </div>
    </main>
  );
}
