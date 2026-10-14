"use client";

import type React from "react";
import type { DayTotals } from "@/lib/api";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

type DonutProps = {
  label: string;
  consumed: number;
  target: number;
  color: string;
  size: number;
  loud?: boolean;
};

/** Remaining-vs-target ring (§7.4): ring fills with consumed/target, the
 *  center number is what's left. */
function Donut({ label, consumed, target, color, size, loud }: DonutProps) {
  const stroke = loud ? 10 : 7;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const hasTarget = target > 0;
  const frac = hasTarget ? Math.min(consumed / target, 1) : 0;
  const remaining = hasTarget ? Math.max(target - consumed, 0) : 0;
  const centerNum = hasTarget ? remaining : consumed;
  const centerSub = hasTarget ? "left" : "logged";

  return (
    <div className={`donut${loud ? " loud" : ""}`}>
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
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
          {fmt.format(centerNum)}g
        </text>
        <text
          className="center-sub"
          x="50%"
          y={loud ? "66%" : "70%"}
          textAnchor="middle"
          fontSize={loud ? 11 : 9}
        >
          {centerSub}
        </text>
      </svg>
      <span
        className="macro-label"
        style={{ "--dot": color } as React.CSSProperties}
      >
        {label}
      </span>
    </div>
  );
}

export default function Tracker({ totals }: { totals: DayTotals | null }) {
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
  // Oura's activity burn, already inside the kcal and carb targets — said out
  // loud so a target that moved during the day is never a mystery.
  const exercise = totals?.exercise_kcal ?? 0;

  return (
    <section className="tracker" aria-label="Daily tracker">
      <div>
        <div className="kcal-row">
          <div>
            <span className="label">Calories</span>
            <span className="figures num">
              <strong>{fmt.format(kcal)}</strong>
              {kcalTarget > 0 ? (
                <span className="of">/ {fmt.format(kcalTarget)} kcal</span>
              ) : (
                <span className="of">kcal</span>
              )}
            </span>
          </div>
          {kcalTarget > 0 ? (
            <span className={`left num${kcalLeft < 0 ? " over" : ""}`}>
              <strong>{fmt.format(Math.abs(kcalLeft))}</strong>
              {kcalLeft < 0 ? "over" : "left"}
            </span>
          ) : null}
        </div>
        <div className="kcal-bar" style={{ marginTop: 12 }}>
          <div
            className={`fill${kcalFrac > 1 ? " over" : ""}`}
            style={{ width: `${Math.min(kcalFrac, 1) * 100}%` }}
          />
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
        />
        <Donut
          label="Protein"
          consumed={protein}
          target={proteinTarget}
          color="var(--mint)"
          size={116}
          loud
        />
        <Donut
          label="Fat"
          consumed={fat}
          target={fatTarget}
          color="var(--fat)"
          size={84}
        />
      </div>

      {!hasAnyTarget ? (
        <p className="zero-hint">No targets set yet — showing what you log.</p>
      ) : null}
    </section>
  );
}
