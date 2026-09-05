"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getDiary, type DayTotals } from "@/lib/api";
import "@/app/history/history.css";
import type { DiaryMeals } from "./DiaryApi";
import DiaryList from "./DiaryList";
import { longDate, parseISO } from "./HistoryApi";
import Tracker from "./Tracker";

/** §7.4: tap any History day → its diary, entries editable per §5. The tab
 *  bar mounts from app/history/layout.tsx. */
export default function HistoryDay({ date }: { date: string }) {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [meals, setMeals] = useState<DiaryMeals | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
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
  }, [date]);

  return (
    <main className="shell day">
      <header className="brand">
        <h1>
          <Link href="/history" className="back-link" aria-label="Back to history">
            ‹
          </Link>
          {longDate(parseISO(date))}
        </h1>
      </header>

      <Tracker totals={totals} />

      {error ? <p className="hist-error">{error}</p> : null}
      {meals ? (
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
