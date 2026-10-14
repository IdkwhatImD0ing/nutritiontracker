"use client";

import "@/app/history/charts.css";
import { fitLabels } from "./ChartBars";

export type StripCell = {
  key: string;
  /** tooltip */
  hint: string;
  /** 0..1 fill (days logged / 7) */
  level: number;
  /** label printed under this cell (e.g. a month boundary) */
  label?: string;
  muted?: boolean;
};

type Props = {
  title: string;
  cells: StripCell[];
  onTap?: (key: string) => void;
};

const CELL_H = 18;
const GAP = 2;
const LABEL_H = 16;
/** a cell's fill: a week with any day logged never fades to nothing */
const cellOpacity = (level: number) => 0.25 + 0.75 * Math.min(level, 1);

/** §7.4 year view: the adherence strip — one cell per week, opacity = share
 *  of days with ≥1 entry. Read-only, like the year's bars: every range ends
 *  today, so a week can't open in the W view, and a day older than the M
 *  view is reached from the day view's date picker (owner, 2026-10-13). */
export default function ChartStrip({ title, cells, onTap }: Props) {
  const n = Math.max(cells.length, 1);
  const W = 360;
  const cw = (W - GAP * (n - 1)) / n;
  const H = CELL_H + LABEL_H;
  // ChartBars' overlap guard; these labels hang right from the cell's left edge
  const labelled = fitLabels(
    cells.map((c, i) => ({ x: i * (cw + GAP), text: c.label ?? "" })),
    "start",
  );
  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} className="chart-svg">
        {cells.map((c, i) => {
          const x = i * (cw + GAP);
          return (
            <g
              key={c.key}
              className={`chart-strip-cell${onTap ? " tap" : ""}${c.muted ? " muted" : ""}`}
              onClick={onTap ? () => onTap(c.key) : undefined}
              role={onTap ? "button" : undefined}
              tabIndex={onTap ? 0 : undefined}
              onKeyDown={
                onTap
                  ? (e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        onTap(c.key);
                      }
                    }
                  : undefined
              }
            >
              <title>{c.hint}</title>
              <rect x={x} y={0} width={cw} height={CELL_H} rx={2} className="chart-strip-bg" />
              {c.level > 0 ? (
                <rect
                  x={x}
                  y={0}
                  width={cw}
                  height={CELL_H}
                  rx={2}
                  fill="var(--mint)"
                  opacity={cellOpacity(c.level)}
                />
              ) : null}
              {labelled.has(i) ? (
                <text className="chart-xlabel" x={x} y={H - 3} textAnchor="start">
                  {c.label}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      {/* the opacity scale's key: 1, 4 and 7 days of a week */}
      <div className="chart-legend">
        Days logged
        {[1, 4, 7].map((days) => (
          <span key={days} className="chart-legend-item">
            <span
              className="chart-legend-swatch cell"
              style={{ background: "var(--mint)", opacity: cellOpacity(days / 7) }}
            />
            {days}
          </span>
        ))}
      </div>
    </figure>
  );
}
