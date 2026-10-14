"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { getDiary, type DayTotals } from "@/lib/api";
import "@/app/history/history.css";
import BackBar, { markReplace } from "./BackBar";
import type { DiaryMeals } from "./DiaryApi";
import DiaryList from "./DiaryList";
import { addDays, isoDate, longDate, parseISO } from "./HistoryApi";
import { ChevronDown, ChevronLeft, ChevronRight } from "./icons";
import Tracker from "./Tracker";

/** A floor for the day picker. Typing a year into a desktop date field
 *  passes through 0002, 0020 and 0202 on the way to 2026; each is a whole
 *  date, and without a floor each would navigate. */
const MIN_DAY = "2000-01-01";

/** §7.4: tap any History day → its diary, entries editable per §5. The tab
 *  bar mounts from app/history/layout.tsx. */
export default function HistoryDay({ date }: { date: string }) {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [meals, setMeals] = useState<DiaryMeals | null>(null);
  const [error, setError] = useState<string | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry (the
  // Library pattern) rather than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  const router = useRouter();
  // Read after mount: the server's clock is UTC, so a server-rendered
  // "is this today?" would disagree with the phone's every evening.
  const [today, setToday] = useState<string | null>(null);
  useEffect(() => setToday(isoDate(new Date())), []);

  // Stepping days replaces rather than pushes, so back still returns to
  // wherever the day view was opened from (History, Today's View link), not
  // to the previous day. markReplace tells BackBar's trail the same.
  const go = (to: string) => {
    markReplace();
    router.replace(`/history/${to}`);
  };
  const step = (n: number) => go(isoDate(addDays(parseISO(date), n)));

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
        <h1>
          {/* The date is the way to any day, however far back (owner,
              2026-10-13): a native date input laid invisibly over it, so a
              tap opens the OS's own picker. */}
          <span className="day-pick">
            {longDate(parseISO(date))}
            <ChevronDown />
            <input
              type="date"
              value={date}
              min={MIN_DAY}
              max={today ?? undefined}
              aria-label="Choose a day"
              // a desktop browser opens its calendar from the icon only;
              // a phone opens its picker on the tap itself
              onClick={(e) => {
                try {
                  e.currentTarget.showPicker();
                } catch {
                  /* unsupported, or already showing */
                }
              }}
              onChange={(e) => {
                const v = e.target.value;
                if (v && v !== date && v >= MIN_DAY && (!today || v <= today)) go(v);
              }}
            />
          </span>
        </h1>
        <div className="day-step">
          <button type="button" aria-label="Previous day" onClick={() => step(-1)}>
            <ChevronLeft />
          </button>
          <button
            type="button"
            aria-label="Next day"
            disabled={!today || date >= today}
            onClick={() => step(1)}
          >
            <ChevronRight />
          </button>
        </div>
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
