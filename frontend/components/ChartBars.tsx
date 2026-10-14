"use client";

import { useState, type KeyboardEvent, type PointerEvent } from "react";
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
  /** scrub-to-select (M, Y: bars too thin to tap): a press or drag picks a
   *  bar, an HTML readout above the plot shows its hint, and `onTap` moves
   *  to the readout's Open button — the bars themselves never navigate */
  select?: boolean;
  format?: (v: number) => string;
  /** centred on the plot when no bar is above zero (as ChartLines' own) */
  emptyText?: string;
};

const W = 360;
const PAD = { l: 44, r: 10, t: 16, b: 22 };
const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
/** Chart text size in viewBox units (charts.css .chart-xlabel and friends):
 *  the 360-unit viewBox draws at ~0.86-0.90 on a phone, so 12.5 renders at
 *  ~11px. fitLabels estimates label widths from it. */
export const CHART_TEXT_PX = 12.5;

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
 *  theme. Bars are tap targets when `onTap` is given (→ that day's diary),
 *  unless `select` routes the tap through the readout instead. */
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
  select = false,
  format = (v) => fmt0.format(v),
  emptyText,
}: Props) {
  const [selKey, setSelKey] = useState<string | null>(null);
  const [hoverKey, setHoverKey] = useState<string | null>(null);
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
  const hintOf = (b: Bar) =>
    b.hint ?? `${b.label}: ${b.value == null ? "—" : format(b.value)}${unit ? ` ${unit}` : ""}`;

  // select mode: the picked bar by key (so a new range keeps nothing stale),
  // else today's, else the newest. A mouse's hover only previews: leaving the
  // plot goes back to the bar last pressed, so the way up to Open (which
  // crosses other bars) doesn't change the day it opens.
  const picked = bars.findIndex((b) => b.key === (hoverKey ?? selKey));
  const today = bars.findIndex((b) => b.active);
  const selIdx = !select ? -1 : picked >= 0 ? picked : today >= 0 ? today : bars.length - 1;
  const sel = selIdx >= 0 ? bars[selIdx] : undefined;
  const tap = select ? undefined : onTap;
  // a press, a drag, or a mouse's hover: the bar under the pointer's x
  const scrub = (e: PointerEvent<SVGSVGElement>) => {
    const hover = e.buttons === 0;
    if (hover && e.pointerType !== "mouse") return;
    const r = e.currentTarget.getBoundingClientRect();
    const i = Math.floor((((e.clientX - r.left) / r.width) * W - PAD.l) / group);
    const b = bars[Math.min(Math.max(i, 0), bars.length - 1)];
    if (!b) return;
    if (hover) {
      setHoverKey(b.key);
    } else {
      setSelKey(b.key);
      setHoverKey(null);
    }
  };
  const step = (e: KeyboardEvent<SVGSVGElement>) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      const i = Math.min(Math.max(selIdx + (e.key === "ArrowRight" ? 1 : -1), 0), bars.length - 1);
      if (bars[i]) setSelKey(bars[i].key);
      setHoverKey(null);
    } else if (e.key === "Enter" && onTap && sel) {
      e.preventDefault();
      onTap(sel.key);
    }
  };

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      {sel ? (
        <div className="chart-readout">
          <span className="num">{hintOf(sel)}</span>
          {onTap ? (
            <button type="button" className="chart-readout-open" onClick={() => onTap(sel.key)}>
              Open ›
            </button>
          ) : null}
        </div>
      ) : null}
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role={select ? "slider" : "img"}
        aria-label={title}
        className="chart-svg"
        tabIndex={select ? 0 : undefined}
        aria-valuemin={select ? 1 : undefined}
        aria-valuemax={select ? bars.length : undefined}
        aria-valuenow={sel ? selIdx + 1 : undefined}
        aria-valuetext={sel ? hintOf(sel) : undefined}
        style={select ? { touchAction: "pan-y" } : undefined}
        onPointerDown={select ? scrub : undefined}
        onPointerMove={select ? scrub : undefined}
        onPointerLeave={select ? () => setHoverKey(null) : undefined}
        onKeyDown={select ? step : undefined}
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
          const cls = `chart-bar${tap ? " tap" : ""}${b.muted ? " muted" : ""}${
            b.active ? " active" : ""
          }${i === selIdx ? " selected" : ""}`;
          return (
            <g
              key={b.key}
              className={cls}
              onClick={tap ? () => tap(b.key) : undefined}
              role={tap ? "button" : undefined}
              tabIndex={tap ? 0 : undefined}
              onKeyDown={
                tap
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        tap(b.key);
                      }
                    }
                  : undefined
              }
            >
              <title>{hintOf(b)}</title>
              {/* full-height hit area so thin bars are still tappable; in
                  select mode it is the picked bar's column band */}
              <rect
                className="chart-hit"
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
            if (!showValues) return null;
            const has = v != null && v > 0;
            // an unlogged day says so with a faint dash, not a missing value
            return has || b.muted ? (
              <text
                key={b.key}
                className={`chart-value${has ? "" : " none"}`}
                x={barX(i) + barW / 2}
                y={(has ? y(v) : y(0)) - 4}
                textAnchor="middle"
              >
                {has ? format(v) : "—"}
              </text>
            ) : null;
          })}
          {bars.map((b, i) =>
            labelled.has(i) ? (
              <text
                key={b.key}
                className={`chart-xlabel${b.active ? " today" : ""}`}
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
          {emptyText && !values.some((v) => v > 0) ? (
            <text
              className="chart-empty"
              x={PAD.l + plotW / 2}
              y={PAD.t + plotH / 2}
              textAnchor="middle"
            >
              {emptyText}
            </text>
          ) : null}
        </g>
      </svg>
    </figure>
  );
}
