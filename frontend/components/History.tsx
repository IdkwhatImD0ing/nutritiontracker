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
  const [today] = useState(() => isoDate(new Date()));
  const [kind, setKind] = useState<RangeKind>(initialKind);
  const [range, setRange] = useState<HistoryRange | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchHistory(kind, today)
      .then((r) => {
        if (!cancelled) setRange(r);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setRange(null);
        setError(
          e instanceof HistoryUnavailable
            ? "The year view needs GET /api/history — not wired yet."
            : "Couldn't load this range.",
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, today, attempt]);

  const streak = range?.streak ?? null;

  return (
    <main className="shell history">
      <header className="brand">
        <h1>History</h1>
        {streak != null ? (
          <span
            className={`streak-chip${streak > 0 ? " live" : ""}`}
            title="Consecutive days with at least one entry"
          >
            {streak === 0 ? "no streak" : `${streak}-day streak`}
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

      {range && range.range === kind ? (
        <p className="range-label">{spanLabel(range)}</p>
      ) : null}

      {error ? (
        <ErrorNotice message={error} onRetry={() => setAttempt((n) => n + 1)} />
      ) : null}
      {loading && !range ? (
        <p className="hhint">
          <span className="spinner" />
          Loading…
        </p>
      ) : null}
      {range ? (
        <div className={loading ? "hist-stale" : undefined}>
          <HistoryView
            kind={range.range}
            range={range}
            today={today}
            onDay={(date) => router.push(`/history/${date}`)}
          />
        </div>
      ) : null}
    </main>
  );
}
