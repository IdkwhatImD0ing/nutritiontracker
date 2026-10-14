"use client";

import { useEffect, useState } from "react";
import { getDiary, type DayTotals } from "@/lib/api";
import "@/app/history/history.css";
import BackBar from "./BackBar";
import type { DiaryMeals } from "./DiaryApi";
import DiaryList from "./DiaryList";
import { isoDate, longDate, parseISO } from "./HistoryApi";
import Tracker from "./Tracker";

/** §7.4: tap any History day → its diary, entries editable per §5. The tab
 *  bar mounts from app/history/layout.tsx. */
export default function HistoryDay({ date }: { date: string }) {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [meals, setMeals] = useState<DiaryMeals | null>(null);
  const [error, setError] = useState<string | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry (the
  // Library pattern) rather than asking for a page reload.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getDiary(date)
      .then((d) => {
        if (cancelled) return;
        setTotals(d.totals);
        setMeals(d.meals);
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't load this day.");
      });
    return () => {
      cancelled = true;
    };
  }, [date, attempt]);

  return (
    <main className="shell day">
      <header className="brand">
        <BackBar parent="/history" label="History" />
        <h1>{longDate(parseISO(date))}</h1>
      </header>

      {/* A failed read is not an empty day: the error replaces the tracker
          rather than sitting under a card of zeros. */}
      {error ? (
        <div className="notice error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="notice-btn"
            onClick={() => setAttempt((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      ) : (
        // before today, the tracker says under/over, not "left"; the first
        // render is the loading state either way, so the server's zone
        // can't make it disagree with the browser's
        <Tracker totals={totals} past={date < isoDate(new Date())} />
      )}

      {meals && !error ? (
        <DiaryList
          meals={meals}
          emptyText="Nothing logged this day."
          onPatched={(entry, t) => {
            setTotals(t);
            setMeals((cur) => {
              if (!cur) return cur;
              const next: DiaryMeals = {};
              for (const [meal, list] of Object.entries(cur)) {
                next[meal] = list.map((e) => (e.id === entry.id ? entry : e));
              }
              return next;
            });
          }}
          onDeleted={(id, t) => {
            setTotals(t);
            setMeals((cur) => {
              if (!cur) return cur;
              const next: DiaryMeals = {};
              for (const [meal, list] of Object.entries(cur)) {
                next[meal] = list.filter((e) => e.id !== id);
              }
              return next;
            });
          }}
        />
      ) : null}
    </main>
  );
}
