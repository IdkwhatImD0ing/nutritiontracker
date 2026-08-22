"use client";

import "@/app/history/charts.css";
import { niceMax } from "./ChartBars";

export type Series = {
  name: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
};

type Props = {
  title: string;
  /** x labels, one per point (render every `labelEvery`) */
  labels: string[];
  series: Series[];
  /** fixed top of the y axis (e.g. 1 for rates); default = nice max of data */
  yMax?: number;
  format?: (v: number) => string;
  /** a dashed reference line (e.g. the Cronometer baseline) */
  baseline?: { value: number; label: string } | null;
  height?: number;
  labelEvery?: number;
  /** shown under the title when every point is null */
  emptyText?: string;
};

const W = 360;
const PAD = { l: 40, r: 10, t: 12, b: 20 };
const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** Hand-rolled SVG multi-series line chart (§9 weekly series). Null points
 *  break the line rather than reading as zero — an empty week is not a
 *  zero-latency week. */
export default function ChartLines({
  title,
  labels,
  series,
  yMax,
  format = (v) => fmt0.format(v),
  baseline = null,
  height = 160,
  labelEvery,
  emptyText = "No data in this range yet.",
}: Props) {
  const H = height;
  const plotW = W - PAD.l - PAD.r;
  const plotH = H - PAD.t - PAD.b;
  const n = Math.max(labels.length, 1);
  const all = series.flatMap((s) => s.values).filter((v): v is number => v != null);
  const empty = all.length === 0;
  const top =
    yMax ?? niceMax(Math.max(...all, baseline?.value ?? 0, 0) * 1.1);
  const x = (i: number) => PAD.l + (n === 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) => PAD.t + plotH - (Math.min(v, top) / top) * plotH;
  const every = labelEvery ?? Math.max(1, Math.ceil(n / 6));
  const ticks = [0, top / 2, top];

  const pathFor = (values: (number | null)[]) => {
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
    return d.trim();
  };

  return (
    <figure className="chart">
      <figcaption className="chart-title">{title}</figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={title} className="chart-svg">
        <g className="chart-grid">
          {ticks.map((t) => (
            <line key={t} x1={PAD.l} x2={W - PAD.r} y1={y(t)} y2={y(t)} />
          ))}
        </g>
        <g className="chart-axis">
          {empty
            ? null
            : ticks.map((t) => (
                <text key={t} x={PAD.l - 6} y={y(t) + 3} textAnchor="end">
                  {format(t)}
                </text>
              ))}
          {labels.map((l, i) =>
            l && i % every === 0 ? (
              <text
                key={`${l}-${i}`}
                className="chart-xlabel"
                x={x(i)}
                y={H - 6}
                textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}
              >
                {l}
              </text>
            ) : null,
          )}
        </g>
        {baseline && baseline.value > 0 ? (
          <g className="chart-target">
            <line x1={PAD.l} x2={W - PAD.r} y1={y(baseline.value)} y2={y(baseline.value)} />
            <text x={W - PAD.r} y={y(baseline.value) - 4} textAnchor="end">
              {baseline.label} {format(baseline.value)}
            </text>
          </g>
        ) : null}
        {series.map((s) => (
          <g key={s.name} className="chart-series">
            <path
              d={pathFor(s.values)}
              fill="none"
              stroke={s.color}
              strokeWidth={2}
              strokeLinejoin="round"
              strokeLinecap="round"
              strokeDasharray={s.dashed ? "5 4" : undefined}
            />
            {s.values.map((v, i) =>
              v == null ? null : (
                <circle key={i} cx={x(i)} cy={y(v)} r={2.6} fill={s.color}>
                  <title>{`${labels[i] ?? ""} · ${s.name}: ${format(v)}`}</title>
                </circle>
              ),
            )}
          </g>
        ))}
        {empty ? (
          <text className="chart-empty" x={W / 2} y={PAD.t + plotH / 2} textAnchor="middle">
            {emptyText}
          </text>
        ) : null}
      </svg>
      <div className="chart-legend">
        {series.map((s) => (
          <span key={s.name} className="chart-legend-item">
            <span
              className={`chart-legend-swatch${s.dashed ? " dashed" : ""}`}
              style={{ background: s.color }}
            />
            {s.name}
          </span>
        ))}
        {baseline && baseline.value > 0 ? (
          <span className="chart-legend-item">
            <span className="chart-legend-swatch dashed" style={{ background: "var(--amber)" }} />
            {baseline.label}
          </span>
        ) : null}
      </div>
    </figure>
  );
}
