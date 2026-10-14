"use client";

import { useEffect, useState } from "react";
import { getWeights, type WeightEntry } from "@/lib/api";

const LB_PER_KG = 2.2046226; // WizardMath's constant; the card shows the unit the weigh-in was said in
const DAYS = 30;
const W = 96;
const H = 34;
/** the sparkline's least y range (~2.2 lb; the series is kg in either unit),
 *  so a few tenths of scale noise don't draw a cliff across the full height */
const MIN_SPAN_KG = 1;

type Unit = WeightEntry["unit"];

function shown(kg: number, unit: Unit): number {
  return Math.round((unit === "lb" ? kg * LB_PER_KG : kg) * 10) / 10;
}

const pad = (n: number) => String(n).padStart(2, "0");

function isoDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function parseISO(s: string): Date {
  const [y, m, d] = s.split("-").map(Number);
  return new Date(y, (m ?? 1) - 1, d ?? 1);
}

function daysBetween(a: string, b: string): number {
  return Math.round((parseISO(b).getTime() - parseISO(a).getTime()) / 86_400_000);
}

/** "today", "yesterday", else "Tue, Sep 2" — with no-break spaces, so a
 *  wrapping line never strands the day number ("Sep / 14"). */
function when(day: string, today: string): string {
  const ago = daysBetween(day, today);
  if (ago === 0) return "today";
  if (ago === 1) return "yesterday";
  return parseISO(day)
    .toLocaleDateString("en-US", {
      weekday: "short",
      month: "short",
      day: "numeric",
    })
    .replace(/ /g, "\u00a0");
}

type Data = { latest: WeightEntry | null; entries: WeightEntry[] };

/** §6.4 weight tracking: the latest weigh-in, the change since the one
 *  before it, and a 30-day sparkline. Reads GET /api/weights; `refreshKey`
 *  re-reads it (Today bumps it when an agent log recorded a weigh-in). A
 *  read that fails renders nothing — never a stale or invented number. */
export default function WeightCard({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<Data | null>(null);
  // Derived per render, not frozen in state: an installed PWA lives across
  // midnight, and a `useState` initialiser would have gone on calling
  // yesterday's weigh-in today's for as long as the app stayed open. Today
  // bumps `refreshKey` when it comes back to the foreground on a new date.
  const today = isoDate(new Date());

  useEffect(() => {
    let cancelled = false;
    getWeights(DAYS)
      .then((r) => {
        if (!cancelled) setData(r);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  if (!data) return null;
  const latest = data.latest;
  if (!latest) {
    return (
      <section className="weight-card" aria-label="Weight">
        <div className="weight-main">
          <span className="label">Weight</span>
          <p className="weight-empty">No weigh-ins yet — tell the agent your weight.</p>
        </div>
      </section>
    );
  }

  const unit = latest.unit;
  // the route answers newest first; the sparkline wants oldest first. The
  // window can be empty while `latest` is older than it — then there is no
  // sparkline, only the number and its date.
  const series = [...data.entries].reverse();
  const last = series.at(-1) ?? null;
  const prev = series.filter((e) => e.day < latest.day).at(-1) ?? null;
  const value = shown(latest.weight_kg, unit);
  const delta = prev ? Math.round((value - shown(prev.weight_kg, unit)) * 10) / 10 : null;
  const change =
    prev && delta != null
      ? delta === 0
        ? `same as ${when(prev.day, today)}`
        : `${delta > 0 ? "up" : "down"} ${Math.abs(delta).toFixed(1)}\u00a0${unit} since ${when(prev.day, today)}`
      : null;

  // sparkline: x by day within the 30-day window, y by the window's range,
  // widened about its middle to at least MIN_SPAN_KG (a flat series sits
  // mid-height)
  const start = isoDate(new Date(parseISO(today).getTime() - (DAYS - 1) * 86_400_000));
  const kgs = series.map((e) => e.weight_kg);
  const mid = (Math.min(...kgs) + Math.max(...kgs)) / 2;
  const lo = Math.min(...kgs, mid - MIN_SPAN_KG / 2);
  const hi = Math.max(...kgs, mid + MIN_SPAN_KG / 2);
  const x = (day: string) => 3 + (Math.max(0, daysBetween(start, day)) / (DAYS - 1)) * (W - 6);
  const y = (kg: number) => 3 + (1 - (kg - lo) / (hi - lo)) * (H - 6);
  const path = series
    .map((e, i) => `${i === 0 ? "M" : "L"}${x(e.day).toFixed(1)},${y(e.weight_kg).toFixed(1)}`)
    .join(" ");

  return (
    <section className="weight-card" aria-label="Weight">
      <div className="weight-main">
        <span className="label">Weight</span>
        <div className="weight-num num">
          <strong>{value.toFixed(1)}</strong>
          <span className="unit">{unit}</span>
        </div>
        <div className="weight-sub">
          {when(latest.day, today)}
          {change ? ` · ${change}` : ""}
        </div>
      </div>
      {last ? (
        <svg
          className="weight-spark"
          viewBox={`0 0 ${W} ${H}`}
          aria-label={`Weight, last ${DAYS} days`}
          role="img"
        >
          {series.length > 1 ? (
            <path
              d={path}
              fill="none"
              stroke="var(--mint)"
              strokeWidth={1.6}
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          ) : null}
          <circle cx={x(last.day)} cy={y(last.weight_kg)} r={2.4} fill="var(--mint)" />
        </svg>
      ) : null}
    </section>
  );
}
