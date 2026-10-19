"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
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

/** Keys that edit one segment of a date field (Alt+↓ opens its calendar). */
const SEGMENT_KEY = /^(\d|ArrowUp|ArrowDown|Backspace|Delete)$/;

type Refocus = "prev" | "next" | "pick" | "back" | null;
/** The control that moved to the day now loading, and that day. Each move
 *  replaces the [date] segment, which remounts this view and drops focus to
 *  <body>, so the new view puts it back (markReplace's pattern: set before
 *  the move). Only that day's view: a move cut short by Back would
 *  otherwise focus a later, unrelated open of the day view. */
let refocus: { to: string; from: Refocus } | null = null;

/** The phone's date never announces a change here; it is read per render. */
const noSubscribe = () => () => {};

/** §7.4: tap any History day → its diary, entries editable per §5. The tab
 *  bar mounts from app/history/layout.tsx. */
export default function HistoryDay({ date, year }: { date: string; year: string }) {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [meals, setMeals] = useState<DiaryMeals | null>(null);
  const [error, setError] = useState<string | null>(null);
  // `attempt` re-runs the read: the failure notice carries a Retry (the
  // Library pattern) rather than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  const router = useRouter();
  // The phone's date, never the server's: its clock is UTC, so a
  // server-rendered "is this today?" would disagree with the phone's every
  // evening. Hydration reads null; a client mount (every step remounts this
  // view) reads it in its first render, so the title and Next don't flash.
  const today = useSyncExternalStore(noSubscribe, () => isoDate(new Date()), () => null);

  // Stepping days replaces rather than pushes, so back still returns to
  // wherever the day view was opened from (History, Today's View link), not
  // to the previous day. markReplace tells BackBar's trail the same.
  const go = (to: string, from: Refocus) => {
    refocus = { to, from };
    markReplace();
    router.replace(`/history/${to}`);
  };
  const step = (n: number) => go(isoDate(addDays(parseISO(date), n)), n < 0 ? "prev" : "next");

  const prevRef = useRef<HTMLButtonElement>(null);
  const nextRef = useRef<HTMLButtonElement>(null);
  const pickRef = useRef<HTMLInputElement>(null);
  // After `today` is read: Next is disabled until then, and stays disabled
  // once a step lands on today, where Previous takes the focus instead.
  useEffect(() => {
    if (!today || refocus?.to !== date) return;
    const { from } = refocus;
    refocus = null;
    if (!from) return;
    // "back": a typed date left by Shift+Tab, for the new view's back link
    const el =
      from === "back"
        ? document.querySelector<HTMLElement>("main.day .nav-back")
        : (from === "pick" ? pickRef : from === "next" && date < today ? nextRef : prevRef).current;
    el?.focus({ preventScroll: true });
  }, [today, date]);

  // A desktop date field reports every segment edit that makes a whole
  // date as a change: "03" typed into Oct 10's month is already Mar 10. So
  // a typed date waits for Enter or for focus to leave the field; a pick
  // from a calendar or a phone's picker still goes at once. `via` is how
  // the field was last worked: a segment edit, another key, or a pointer.
  const via = useRef<"edit" | "key" | null>(null);
  const typed = useRef(false);
  const choose = (input: HTMLInputElement, from: Refocus) => {
    typed.current = false;
    const v = input.value;
    const ok = !!v && v !== date && v >= MIN_DAY && (!today || v <= today);
    if (ok) go(v, from);
    else input.value = date;
    return ok;
  };
  // A mouse press on Previous or Next takes focus from a typed date's field
  // before its click lands: the blur opens the typed day, then the click,
  // stepping from the day still on screen, replaces it. So the press opens
  // the typed day itself and its click is dropped. The press, not the blur's
  // relatedTarget, because Safari doesn't focus a clicked button. A press on
  // a disabled Next still gets a pointerdown; it is left to the blur.
  const dropClick = useRef(false);
  const press = (from: "prev" | "next") => {
    const input = pickRef.current;
    const off = from === "next" && !!nextRef.current?.disabled;
    dropClick.current = typed.current && !!input && !off && choose(input, from);
  };
  const click = (n: number) => {
    if (dropClick.current) dropClick.current = false;
    else step(n);
  };

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
            {/* the year once it isn't this one: the phone's, as `today`
                is; until that is read, the server's `year` (the tab
                title's rule), which can differ only around New Year */}
            {longDate(parseISO(date), date.slice(0, 4) !== (today?.slice(0, 4) ?? year))}
            <ChevronDown />
            {/* uncontrolled, so a half-typed date isn't reset under the
                keys; keyed by the day so a new one always shows */}
            <input
              key={date}
              ref={pickRef}
              type="date"
              defaultValue={date}
              min={MIN_DAY}
              max={today ?? undefined}
              aria-label="Choose a day"
              // a desktop browser opens its calendar from the icon only;
              // a phone opens its picker on the tap itself
              onClick={(e) => {
                via.current = null;
                try {
                  e.currentTarget.showPicker();
                } catch {
                  /* unsupported, or already showing */
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  if (typed.current) choose(e.currentTarget, "pick");
                } else {
                  via.current = !e.altKey && SEGMENT_KEY.test(e.key) ? "edit" : "key";
                }
              }}
              onChange={(e) => {
                if (via.current === "edit") typed.current = true;
                // a keyboard user stays on the field; a tap's focus isn't
                // moved, so a phone's picker can't open again by itself
                else choose(e.currentTarget, via.current ? "pick" : null);
              }}
              onBlur={(e) => {
                if (!typed.current) return;
                const to = e.relatedTarget;
                // a press on the back link (focused by it, and :active) is
                // its own way out: the typed day may never open, and a kept
                // "back" would take focus in a later open of that day
                const back = to instanceof Element ? to.closest(".nav-back") : null;
                choose(
                  e.currentTarget,
                  to === prevRef.current
                    ? "prev"
                    : to === nextRef.current
                      ? "next"
                      : back && !back.matches(":active")
                        ? "back"
                        : null,
                );
              }}
            />
          </span>
        </h1>
        <div className="day-step">
          <button
            type="button"
            ref={prevRef}
            aria-label="Previous day"
            onPointerDown={() => press("prev")}
            onClick={() => click(-1)}
          >
            <ChevronLeft />
          </button>
          <button
            type="button"
            ref={nextRef}
            aria-label="Next day"
            disabled={!today || date >= today}
            onPointerDown={() => press("next")}
            onClick={() => click(1)}
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
