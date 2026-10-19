"use client";

import Link from "next/link";
import type React from "react";
import type { DayTotals } from "@/lib/api";
import { ChevronRight } from "./icons";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

type DonutProps = {
  label: string;
  consumed: number;
  target: number;
  color: string;
  size: number;
  loud?: boolean;
  loading?: boolean;
  /** an overage reads amber (carbs, fat). Protein's target is a floor, so
   *  going past it is the goal met, not a warning. */
  warnOver?: boolean;
  /** a closed day: nothing is left to eat, so the center is what was eaten */
  past?: boolean;
};

/** Remaining-vs-target ring (§7.4): ring fills with consumed/target, the
 *  center number is what's left — or, past the target, how far over
 *  ("+16g over") on a full ring. On a past day it is what was eaten, over
 *  "of 180g". */
function Donut({ label, consumed, target, color, size, loud, loading, warnOver, past }: DonutProps) {
  const stroke = loud ? 10 : 7;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const hasTarget = target > 0;
  const frac = hasTarget ? Math.min(consumed / target, 1) : 0;
  const remaining = hasTarget ? Math.max(target - consumed, 0) : 0;
  // rounded like the figure, so 0.3 g past reads "0g left", not "+0g over"
  const over = hasTarget && Math.round(consumed - target) > 0;
  const centerNum = !hasTarget || past ? consumed : over ? consumed - target : remaining;
  const centerSub = !hasTarget
    ? "logged"
    : past
      ? `of ${fmt.format(target)}g`
      : over
        ? "over"
        : "left";
  // One spoken line per ring: the svg's figures and the label under it read
  // as loose fragments ("97, g, left, CARBS"), the number before its macro.
  // It follows the figure's branches: left, over, or a past day's total.
  const spoken = loading
    ? `${label}: loading`
    : hasTarget
      ? `${label}: ${fmt.format(consumed)} of ${fmt.format(target)} g` +
        (over
          ? `, ${fmt.format(consumed - target)} g over`
          : past
            ? ""
            : `, ${fmt.format(remaining)} g left`)
      : `${label}: ${fmt.format(consumed)} g logged`;

  return (
    <div
      className={`donut${loud ? " loud" : ""}${over ? " over" : ""}${over && warnOver ? " warn" : ""}`}
      role="img"
      aria-label={spoken}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} aria-hidden="true">
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke="var(--surface-3)"
          strokeWidth={stroke}
        />
        {frac > 0 ? (
          <circle
            cx={size / 2}
            cy={size / 2}
            r={r}
            fill="none"
            stroke={color}
            strokeWidth={stroke}
            strokeLinecap="round"
            strokeDasharray={`${c * frac} ${c}`}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
          />
        ) : null}
        <text
          className="center-num num"
          x="50%"
          y="48%"
          textAnchor="middle"
          dominantBaseline="middle"
          fontSize={loud ? 22 : 16}
        >
          {loading ? "—" : `${over && !past ? "+" : ""}${fmt.format(centerNum)}g`}
        </text>
        {loading ? null : (
          <text
            className="center-sub"
            x="50%"
            y={loud ? "66%" : "70%"}
            textAnchor="middle"
            fontSize={11}
          >
            {centerSub}
          </text>
        )}
      </svg>
      <span
        className="macro-label"
        style={{ "--dot": color } as React.CSSProperties}
        aria-hidden="true"
      >
        {label}
      </span>
    </div>
  );
}

/** `past`: a History day before today — "under"/"over" its target rather
 *  than "left", which only means something while the day is open. */
export default function Tracker({
  totals,
  past = false,
}: {
  totals: DayTotals | null;
  past?: boolean;
}) {
  // null = the day hasn't been read yet. Same footprint, dashes and empty
  // tracks: a 0 or "No targets yet" would be a claim about a day we
  // haven't seen (it read as a real empty day on every cold open).
  const loading = totals === null;
  const kcal = totals?.kcal ?? 0;
  const protein = totals?.protein_g ?? 0;
  const carb = totals?.carb_g ?? 0;
  const fat = totals?.fat_g ?? 0;
  // Zero-state rule: targets may come back empty ({}) before the goal wizard
  // has run — treat every missing/zero target as "no target set".
  const t = totals?.targets;
  const kcalTarget = t?.kcal ?? 0;
  const proteinTarget = t?.protein_g ?? 0;
  const carbTarget = t?.carb_g ?? 0;
  const fatTarget = t?.fat_g ?? 0;
  const hasAnyTarget =
    kcalTarget > 0 || proteinTarget > 0 || carbTarget > 0 || fatTarget > 0;

  const kcalFrac = kcalTarget > 0 ? kcal / kcalTarget : 0;
  const kcalLeft = kcalTarget - kcal;
  // the Donut's rule, rounded like the figure: 0.4 kcal past reads "0 left"
  const kcalOver = kcalTarget > 0 && Math.round(kcal - kcalTarget) > 0;
  // the calories row and its bar, spoken as one line like each ring
  const kcalSpoken = loading
    ? "Calories: loading"
    : kcalTarget > 0
      ? `Calories: ${fmt.format(kcal)} of ${fmt.format(kcalTarget)} kcal, ` +
        `${fmt.format(Math.abs(kcalLeft))} ${kcalOver ? "over" : past ? "under" : "left"}`
      : `Calories: ${fmt.format(kcal)} kcal`;
  // Oura's activity burn, already inside the kcal and carb targets — said out
  // loud so a target that moved during the day is never a mystery.
  const exercise = totals?.exercise_kcal ?? 0;

  return (
    <section
      className={`tracker${loading ? " is-loading" : ""}`}
      aria-label="Daily tracker"
      aria-busy={loading || undefined}
    >
      <div>
        {/* the line under the bar sits outside the role=img, which would
            hide it from a screen reader */}
        <div role="img" aria-label={kcalSpoken}>
          <div className="kcal-row">
            <div>
              <span className="label">Calories</span>
              <span className="figures num">
                <strong>{loading ? "—" : fmt.format(kcal)}</strong>
                {kcalTarget > 0 ? (
                  <span className="of">/ {fmt.format(kcalTarget)} kcal</span>
                ) : (
                  <span className="of">kcal</span>
                )}
              </span>
            </div>
            {kcalTarget > 0 ? (
              <span className={`left num${kcalOver ? " over" : ""}`}>
                <strong>{fmt.format(Math.abs(kcalLeft))}</strong>
                {kcalOver ? "over" : past ? "under" : "left"}
              </span>
            ) : null}
          </div>
          {/* no target, no bar: an empty track under 1,270 kcal says nothing */}
          {loading || kcalTarget > 0 ? (
            <div className="kcal-bar" style={{ marginTop: 12 }}>
              <div
                className={`fill${kcalOver ? " over" : ""}`}
                style={{ width: `${Math.min(kcalFrac, 1) * 100}%` }}
              />
            </div>
          ) : null}
        </div>
        {exercise > 0 ? (
          <p className="zero-hint" style={{ marginTop: 8 }}>
            Includes {fmt.format(exercise)} kcal of activity from Oura
          </p>
        ) : null}
      </div>

      <div className="donuts">
        <Donut
          label="Carbs"
          consumed={carb}
          target={carbTarget}
          color="var(--carb)"
          size={84}
          loading={loading}
          warnOver
          past={past}
        />
        <Donut
          label="Protein"
          consumed={protein}
          target={proteinTarget}
          color="var(--mint)"
          size={116}
          loud
          loading={loading}
          past={past}
        />
        <Donut
          label="Fat"
          consumed={fat}
          target={fatTarget}
          color="var(--fat)"
          size={84}
          loading={loading}
          warnOver
          past={past}
        />
      </div>

      {!loading && !hasAnyTarget ? (
        <p className="zero-hint">
          No targets yet.{" "}
          <Link href="/settings?setup=targets" className="m3-link-btn">
            Set them
            <ChevronRight />
          </Link>
        </p>
      ) : null}
    </section>
  );
}
