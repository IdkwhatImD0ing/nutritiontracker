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
  /** fill for a bar past the target (default amber, the kcal overshoot);
   *  null keeps `color`, for a target that is a floor (protein, days logged) */
  overColor?: string | null;
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
/** Chart text size in viewBox units (charts.css .chart-xlabel and friends);
 *  fitLabels estimates label widths from it. */
export const CHART_TEXT_PX = 10;

/** The x labels that fit, as indices into `labels` ("" = none). Walks from
 *  the newest (right) end and keeps a label only when it ends at least 4
 *  units left of the last one kept, so "Oct 1" beats "Sep 30" beside it and
 *  "Nov" a partial first "Oct". `x` is the text's anchor: its centre for
 *  "middle", its left edge for "start". */
export function fitLabels(
  labels: { x: number; text: string }[],
  anchor: "middle" | "start",
): Set<number> {
  const kept = new Set<number>();
  let edge = Infinity;
  for (let i = labels.length - 1; i >= 0; i--) {
    const { x, text } = labels[i];
    if (!text) continue;
    const w = text.length * CHART_TEXT_PX * 0.6;
    const left = anchor === "middle" ? x - w / 2 : x;
    if (left + w + 4 <= edge) {
      kept.add(i);
      edge = left;
    }
  }
  return kept;
}

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
  overColor = "var(--amber)",
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
  const barX = (i: number) => PAD.l + i * group + (group - barW) / 2;
  const labelled = fitLabels(
    bars.map((b, i) => ({ x: barX(i) + barW / 2, text: i % every === 0 ? b.label : "" })),
    "middle",
  );
  // the midline sits exactly where its label says (7 → 0 · 4 · 7, not 3.5)
  const mid = yMax >= 10 ? yMax / 2 : Math.round(yMax / 2);
  // rounding can collapse the midline onto 0 or the top (yMax 1 → mid 1)
  const ticks = [...new Set([0, mid, yMax])];

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={title}
        className="chart-svg"
      >
        <g className="chart-grid">
          {ticks.map((t) => (
            <line key={t} x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
          ))}
        </g>
        <g className="chart-axis">
          {ticks.map((t) => (
            <text key={t} x={PAD.l - 6} y={y(t) + 3} textAnchor="end">
              {format(t)}
            </text>
          ))}
        </g>
        {bars.map((b, i) => {
          const x0 = barX(i);
          const v = b.value;
          const top = v == null ? y(0) : y(v);
          const h = Math.max(0, y(0) - top);
          const over = hasTarget && v != null && v > target;
          const cls = `chart-bar${onTap ? " tap" : ""}${b.muted ? " muted" : ""}${
            b.active ? " active" : ""
          }`;
          return (
            <g
              key={b.key}
              className={cls}
              onClick={onTap ? () => onTap(b.key) : undefined}
              role={onTap ? "button" : undefined}
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
              <title>
                {b.hint ?? `${b.label}: ${v == null ? "—" : format(v)}${unit ? ` ${unit}` : ""}`}
              </title>
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
                  fill={over && overColor ? overColor : color}
                />
              )}
            </g>
          );
        })}
        {hasTarget ? (
          <g className="chart-target" pointerEvents="none">
            {/* a surface band under the dashes keeps the line visible where
                it crosses a bar, amber or not */}
            <line
              className="chart-target-halo"
              x1={PAD.l}
              x2={W - PAD.r}
              y1={y(target)}
              y2={y(target)}
            />
            <line x1={PAD.l} x2={W - PAD.r} y1={y(target)} y2={y(target)} />
          </g>
        ) : null}
        {/* text last: bars, then the target line, then every label, so the
            line never strikes through a value. Taps fall through to the bar. */}
        <g pointerEvents="none">
          {bars.map((b, i) => {
            const v = b.value;
            return showValues && v != null && v > 0 ? (
              <text
                key={b.key}
                className="chart-value"
                x={barX(i) + barW / 2}
                y={y(v) - 4}
                textAnchor="middle"
              >
                {format(v)}
              </text>
            ) : null;
          })}
          {bars.map((b, i) =>
            labelled.has(i) ? (
              <text
                key={b.key}
                className="chart-xlabel"
                x={barX(i) + barW / 2}
                y={H - 6}
                textAnchor="middle"
              >
                {b.label}
              </text>
            ) : null,
          )}
          {hasTarget ? (
            <g className="chart-target">
              <text x={W - PAD.r} y={y(target) - 4} textAnchor="end">
                {targetLabel} {format(target)}
              </text>
            </g>
          ) : null}
        </g>
      </svg>
    </figure>
  );
}
