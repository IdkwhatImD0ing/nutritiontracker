"use client";

/**
 * History data (§7.4 History: 3 days · week · month · year).
 *
 * Reads `GET /api/history?range=3d|w|m|y` (backend/routes/history.py): every
 * range is anchored on today in settings.timezone — 3d = today + 2 prior
 * days (with per-meal subtotals), w = last 7 days, m = last 30 days, y =
 * last 52 weeks as weekly averages over logged days + days_logged. The
 * shapes below are hand-typed against that route until typegen reruns.
 *
 * Until the integrator wires the router into main.py the route 404s; the
 * day ranges (3d/w/m) are then assembled from per-day `GET /api/diary?date=`
 * calls with bounded concurrency, and the year view says it needs the route.
 */

import { ApiError, getDiary, type DayTotals } from "@/lib/api";
import { apiRequest } from "./DiaryApi";

export type RangeKind = "3d" | "w" | "m" | "y";
export type MacroTargets = DayTotals["targets"];

export type MacroSums = {
  kcal: number;
  protein_g: number;
  carb_g: number;
  fat_g: number;
};

export type HistoryDay = MacroSums & {
  date: string; // YYYY-MM-DD, local
  entries: number;
  logged: boolean;
  fiber_g: number | null;
  /** 3d only: per-meal subtotals */
  meals: Record<string, MacroSums> | null;
};

export type HistoryWeek = {
  week_start: string; // Monday
  days_logged: number;
  adherence: number; // days_logged / 7
  /** mean over logged days; null when none */
  avg: MacroSums | null;
};

export type HistoryRange = {
  range: RangeKind;
  start: string;
  end: string; // inclusive (today)
  targets: MacroTargets;
  streak: number;
  baseline_days_per_week: number;
  days: HistoryDay[]; // 3d / w / m
  weeks: HistoryWeek[]; // y
};

export class HistoryUnavailable extends Error {
  constructor() {
    super("GET /api/history is not wired yet — the year view needs it");
  }
}

// ---------------------------------------------------------------------------
// local-date helpers (browser zone; the server's day boundaries follow
// settings.timezone — the same zone in practice for a single user)
// ---------------------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, "0");

export function isoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function parseISO(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, (m ?? 1) - 1, d ?? 1);
}

export function addDays(d: Date, n: number): Date {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
}

export const WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function weekdayShort(d: Date): string {
  return WEEKDAY_SHORT[(d.getDay() + 6) % 7];
}

export function monthShort(d: Date): string {
  return d.toLocaleDateString("en-US", { month: "short" });
}

/** "Thursday, Oct 10", or with `year` "Thu, Oct 10, 2024": the short
 *  weekday keeps the day view's title on one line at 320px. */
export function longDate(d: Date, year = false): string {
  return d.toLocaleDateString("en-US", {
    weekday: year ? "short" : "long",
    month: "short",
    day: "numeric",
    ...(year ? { year: "numeric" } : {}),
  });
}

/** Consecutive logged days ending today (or yesterday, if today is still
 *  empty) — the fallback's version of the route's `streak`. `open` = the run
 *  reaches the start of the loaded window, so the true streak may be longer. */
export function computeStreak(
  days: HistoryDay[],
  today: string,
): { n: number; open: boolean } {
  const byDate = new Map(days.map((d) => [d.date, d.entries]));
  let cursor = parseISO(today);
  if ((byDate.get(isoDate(cursor)) ?? 0) === 0) cursor = addDays(cursor, -1);
  let n = 0;
  while (true) {
    const key = isoDate(cursor);
    if (!byDate.has(key)) return { n, open: n > 0 };
    if ((byDate.get(key) ?? 0) === 0) return { n, open: false };
    n += 1;
    cursor = addDays(cursor, -1);
  }
}

// ---------------------------------------------------------------------------
// fetch
// ---------------------------------------------------------------------------

const DAYS: Record<Exclude<RangeKind, "y">, number> = { "3d": 3, w: 7, m: 30 };
const FALLBACK_CONCURRENCY = 4;

const num = (v: unknown): number => (typeof v === "number" && Number.isFinite(v) ? v : 0);

function sums(v: unknown): MacroSums {
  const r = (v ?? {}) as Record<string, unknown>;
  return {
    kcal: num(r.kcal),
    protein_g: num(r.protein_g),
    carb_g: num(r.carb_g),
    fat_g: num(r.fat_g),
  };
}

function targetsOf(v: unknown): MacroTargets {
  return sums(v);
}

function normalizeRange(raw: unknown, kind: RangeKind): HistoryRange {
  const r = (raw ?? {}) as Record<string, unknown>;
  const days = (Array.isArray(r.days) ? r.days : [])
    .map((d) => d as Record<string, unknown>)
    .filter((d) => typeof d.date === "string")
    .map<HistoryDay>((d) => ({
      ...sums(d),
      date: d.date as string,
      entries: num(d.entries),
      logged: d.logged === true || num(d.entries) > 0,
      fiber_g: typeof d.fiber_g === "number" ? d.fiber_g : null,
      meals:
        d.meals && typeof d.meals === "object"
          ? Object.fromEntries(
              Object.entries(d.meals as Record<string, unknown>).map(([k, v]) => [k, sums(v)]),
            )
          : null,
    }));
  const weeks = (Array.isArray(r.weeks) ? r.weeks : [])
    .map((w) => w as Record<string, unknown>)
    .filter((w) => typeof w.week_start === "string")
    .map<HistoryWeek>((w) => ({
      week_start: w.week_start as string,
      days_logged: num(w.days_logged),
      adherence: num(w.adherence),
      avg: w.avg && typeof w.avg === "object" ? sums(w.avg) : null,
    }));
  return {
    range: kind,
    start: typeof r.start === "string" ? r.start : days[0]?.date ?? weeks[0]?.week_start ?? "",
    end: typeof r.end === "string" ? r.end : days[days.length - 1]?.date ?? "",
    targets: targetsOf(r.targets),
    streak: num(r.streak),
    baseline_days_per_week: num(r.baseline_days_per_week),
    days,
    weeks,
  };
}

async function fetchViaDiary(kind: Exclude<RangeKind, "y">, today: string): Promise<HistoryRange> {
  const n = DAYS[kind];
  const dates = Array.from({ length: n }, (_, i) => isoDate(addDays(parseISO(today), i - (n - 1))));
  const known = new Map<string, HistoryDay>();
  let targets: MacroTargets = targetsOf(null);
  let next = 0;
  const worker = async () => {
    while (next < dates.length) {
      const date = dates[next++];
      const diary = await getDiary(date);
      let entries = 0;
      const meals: Record<string, MacroSums> = {};
      for (const [meal, list] of Object.entries(diary.meals)) {
        entries += list.length;
        if (list.length) {
          meals[meal] = list.reduce<MacroSums>(
            (s, e) => ({
              kcal: s.kcal + e.macros.kcal,
              protein_g: s.protein_g + e.macros.protein_g,
              carb_g: s.carb_g + e.macros.carb_g,
              fat_g: s.fat_g + e.macros.fat_g,
            }),
            { kcal: 0, protein_g: 0, carb_g: 0, fat_g: 0 },
          );
        }
      }
      known.set(date, {
        date,
        kcal: diary.totals.kcal,
        protein_g: diary.totals.protein_g,
        carb_g: diary.totals.carb_g,
        fat_g: diary.totals.fat_g,
        entries,
        logged: entries > 0,
        fiber_g: null,
        meals: kind === "3d" ? meals : null,
      });
      targets = targetsOf(diary.totals.targets);
    }
  };
  await Promise.all(
    Array.from({ length: Math.min(FALLBACK_CONCURRENCY, dates.length) }, worker),
  );
  const days = dates.map(
    (date) =>
      known.get(date) ?? {
        date,
        kcal: 0,
        protein_g: 0,
        carb_g: 0,
        fat_g: 0,
        entries: 0,
        logged: false,
        fiber_g: null,
        meals: null,
      },
  );
  return {
    range: kind,
    start: dates[0],
    end: today,
    targets,
    streak: computeStreak(days, today).n,
    baseline_days_per_week: 0,
    days,
    weeks: [],
  };
}

export async function fetchHistory(kind: RangeKind, today: string): Promise<HistoryRange> {
  const qs = new URLSearchParams({ range: kind });
  try {
    const raw = await apiRequest<unknown>(`/api/history?${qs.toString()}`);
    return normalizeRange(raw, kind);
  } catch (e) {
    // 404/405 = the router isn't wired yet; anything else is a real error
    if (!(e instanceof ApiError) || (e.status !== 404 && e.status !== 405)) throw e;
    if (kind === "y") throw new HistoryUnavailable();
    return fetchViaDiary(kind, today);
  }
}
