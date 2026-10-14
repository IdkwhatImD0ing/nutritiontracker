"use client";

import "@/app/history/charts.css";

export type Bar = {
  key: string;
  /** x-axis label (may be "" to skip) */
  label: string;
  value: number | null;
  /** tooltip text (SVG <title>) */
  hint?: string;
  /** render quietly (e.g. a future day or an unlogged day) */
  muted?: boolean;
  active?: boolean;
};

type Props = {
  title: string;
  bars: Bar[];
  /** dashed target line + label; omitted/0 = no target set */
  target?: number | null;
  targetLabel?: string;
  color?: string;
  unit?: string;
  height?: number;
  /** print the value above each bar (fine for ≤ 14 bars) */
  showValues?: boolean;
  /** x label every n bars (default: auto so labels never collide) */
  labelEvery?: number;
  /** fixed top of the y axis (e.g. 7 for days/week); default = nice max */
  yMax?: number;
  onTap?: (key: string) => void;
  format?: (v: number) => string;
};

const W = 360;
const PAD = { l: 36, r: 10, t: 14, b: 20 };
const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

const NICE = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

/** Smallest "round" number ≥ v (2,860 → 3,000; 281 → 300). */
export function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  const m = v / p;
  return (NICE.find((n) => n >= m) ?? 10) * p;
}

/** Hand-rolled SVG bar chart (§7.4 week/month views, §9 adherence): daily
 *  bars against a dashed target line; tokens only, so it reads on any
 *  theme. Bars are tap targets when `onTap` is given (→ that day's diary). */
export default function ChartBars({
  title,
  bars,
  target,
  targetLabel = "target",
  color = "var(--spruce-soft)",
  unit = "",
  height = 150,
  showValues = false,
  labelEvery,
  yMax: yMaxProp,
  onTap,
  format = (v) => fmt0.format(v),
}: Props) {
  const H = height;
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const values = bars.map((b) => b.value ?? 0);
  const hasTarget = typeof target === "number" && target > 0;
  const rawMax = Math.max(...values, hasTarget ? target : 0, 0);
  const yMax = yMaxProp ?? niceMax(rawMax * 1.08);
  const y = (v: number) => PAD.t + plotH - (Math.min(v, yMax) / yMax) * plotH;
  const n = Math.max(bars.length, 1);
  const group = plotW / n;
  const barW = Math.max(2, group * (n > 20 ? 0.68 : 0.6));
  const every = labelEvery ?? Math.max(1, Math.ceil(n / 8));
  // the midline sits exactly where its label says (7 → 0 · 4 · 7, not 3.5)
  const mid = yMax >= 10 ? yMax / 2 : Math.round(yMax / 2);
  // rounding can collapse the midline onto 0 or the top (yMax 1 → mid 1)
  const ticks = [...new Set([0, mid, yMax])];

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      {/* Tappable bars are buttons, and an img's children are presentational —
          VoiceOver would hear one picture and reach no bar. So a chart you
          can tap is a group of its bars, and its tick text is hidden. */}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role={onTap ? "group" : "img"}
        aria-label={title}
        className="chart-svg"
      >
        <g className="chart-grid" aria-hidden="true">
          {ticks.map((t) => (
            <line key={t} x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
          ))}
        </g>
        <g className="chart-axis" aria-hidden="true">
          {ticks.map((t) => (
            <text key={t} x={PAD.l - 6} y={y(t) + 3} textAnchor="end">
              {format(t)}
            </text>
          ))}
        </g>
        {bars.map((b, i) => {
          const x0 = PAD.l + i * group + (group - barW) / 2;
          const v = b.value;
          const top = v == null ? y(0) : y(v);
          const h = Math.max(0, y(0) - top);
          const over = hasTarget && v != null && v > target;
          const cls = `chart-bar${onTap ? " tap" : ""}${b.muted ? " muted" : ""}${
            b.active ? " active" : ""
          }`;
          const said =
            b.hint ?? `${b.label}: ${v == null ? "—" : format(v)}${unit ? ` ${unit}` : ""}`;
          return (
            <g
              key={b.key}
              className={cls}
              onClick={onTap ? () => onTap(b.key) : undefined}
              role={onTap ? "button" : undefined}
              aria-label={onTap ? said : undefined}
              tabIndex={onTap ? 0 : undefined}
              onKeyDown={
                onTap
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onTap(b.key);
                      }
                    }
                  : undefined
              }
            >
              <title>{said}</title>
              {/* full-height hit area so thin bars are still tappable */}
              <rect
                x={PAD.l + i * group}
                y={PAD.t}
                width={group}
                height={plotH}
                fill="transparent"
              />
              {v == null || h === 0 ? (
                <line
                  x1={x0}
                  x2={x0 + barW}
                  y1={y(0)}
                  y2={y(0)}
                  className="chart-bar-empty"
                />
              ) : (
                <rect
                  x={x0}
                  y={top}
                  width={barW}
                  height={h}
                  rx={Math.min(3, barW / 2)}
                  fill={over ? "var(--amber)" : color}
                />
              )}
              {showValues && v != null && v > 0 ? (
                <text
                  className="chart-value"
                  x={x0 + barW / 2}
                  y={top - 4}
                  textAnchor="middle"
                >
                  {format(v)}
                </text>
              ) : null}
              {b.label && i % every === 0 ? (
                <text
                  className="chart-xlabel"
                  x={x0 + barW / 2}
                  y={H - 6}
                  textAnchor="middle"
                >
                  {b.label}
                </text>
              ) : null}
            </g>
          );
        })}
        {hasTarget ? (
          <g className="chart-target">
            <line x1={PAD.l} x2={W - PAD.r} y1={y(target)} y2={y(target)} />
            <text x={W - PAD.r} y={y(target) - 4} textAnchor="end">
              {targetLabel} {format(target)}
            </text>
          </g>
        ) : null}
      </svg>
    </figure>
  );
}
