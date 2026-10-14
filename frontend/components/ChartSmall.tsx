"use client";

import "@/app/history/charts.css";

type Props = {
  title: string;
  values: (number | null)[];
  format: (v: number) => string;
  color?: string;
  /** direction the number is supposed to move (§9 arrows: tier-3 → 0,
   *  short-circuit ↑) — rendered as a quiet hint, not a judgement */
  goal?: "up" | "down" | null;
};

const W = 100;
const H = 30;

/** §9 secondary series as a small multiple: latest value big, the weekly
 *  sparkline under it. Nulls break the line (no data ≠ zero). */
export default function ChartSmall({
  title,
  values,
  format,
  color = "var(--sage)",
  goal = null,
}: Props) {
  const nums = values.filter((v): v is number => v != null);
  const latestIdx = values.length - 1 - [...values].reverse().findIndex((v) => v != null);
  const latest = nums.length ? values[latestIdx] : null;
  const max = Math.max(...nums, 0);
  const n = Math.max(values.length, 1);
  const x = (i: number) => (n === 1 ? W / 2 : 3 + (i / (n - 1)) * (W - 6));
  const y = (v: number) => (max === 0 ? H - 3 : 3 + (1 - v / max) * (H - 6));
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (v == null) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
    pen = true;
  });

  return (
    <div className="chart-small">
      <div className="chart-small-title">
        <span className="chart-small-name">{title}</span>
        {goal ? (
          <span
            className="chart-small-goal"
            aria-label={`should go ${goal}`}
            title={`should go ${goal}`}
          >
            {goal === "down" ? "↓" : "↑"}
          </span>
        ) : null}
      </div>
      <div className="chart-small-value num">
        {latest == null ? "—" : format(latest)}
      </div>
      {/* stretched to the tile ("none"), so the line spans it rather than
          sitting in the middle as a stub; non-scaling strokes keep the line
          and the end dot (a zero-length round-capped path, which unlike a
          circle stays round when stretched) their drawn width */}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="chart-small-svg"
        aria-hidden="true"
      >
        <path
          d={d.trim()}
          fill="none"
          stroke={color}
          strokeWidth={1.6}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
        {latest != null ? (
          <path
            d={`M${x(latestIdx).toFixed(1)},${y(latest).toFixed(1)}h0`}
            stroke={color}
            strokeWidth={4.4}
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
        ) : null}
      </svg>
    </div>
  );
}
