"use client";

import ChartBars from "./ChartBars";
import ChartLines from "./ChartLines";
import ChartSmall from "./ChartSmall";
import type { MetricsResponse } from "./ExperimentApi";
import { monthShort, parseISO } from "./HistoryApi";

const fmt0 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const pct = (v: number) => `${fmt0.format(v * 100)}%`;
const secs = (ms: number) => `${fmt1.format(ms / 1000)}s`;
const DUPES_SHOWN = 8;

function weekLabel(week: string): string {
  const d = parseISO(week);
  return `${monthShort(d)} ${d.getDate()}`;
}

/** Align a per-week series to the response's week list (a missing week
 *  reads as null, never zero). */
function aligned<T extends { week: string }>(
  weeks: string[],
  points: T[],
  pick: (p: T) => number | null,
): (number | null)[] {
  const by = new Map(points.map((p) => [p.week, p]));
  return weeks.map((w) => {
    const p = by.get(w);
    return p ? pick(p) : null;
  });
}

/** §9: the three primary charts (log latency, alias hit rate, adherence vs
 *  the Cronometer baseline) and the secondary series as small multiples.
 *  Pure — data in from ExperimentApi, nothing fetched here. */
export default function ExperimentCharts({ data }: { data: MetricsResponse }) {
  const weeks = data.weeks;
  const labels = weeks.map(weekLabel);
  const lat = data.latency;
  const hit = data.hit_rate;
  const sec = data.secondary;
  const adherenceBy = new Map(data.adherence.map((p) => [p.week, p.days_logged]));
  const dupes = data.duplicate_suspects;

  return (
    <>
      <section className="exp-section">
        <h2 className="exp-h2">Primary</h2>
        <ChartLines
          title="Log latency · weekly"
          labels={labels}
          format={secs}
          series={[
            { name: "commit p50", color: "var(--mint)", values: aligned(weeks, lat, (p) => p.commit_ms_p50) },
            { name: "commit p90", color: "var(--mint)", dashed: true, values: aligned(weeks, lat, (p) => p.commit_ms_p90) },
            { name: "staged p50", color: "var(--sage)", values: aligned(weeks, lat, (p) => p.staged_ms_p50) },
            { name: "staged p90", color: "var(--sage)", dashed: true, values: aligned(weeks, lat, (p) => p.staged_ms_p90) },
          ]}
        />
        <ChartLines
          title="Alias hit rate · weekly"
          labels={labels}
          yMax={1}
          format={pct}
          series={[
            { name: "committed logs", color: "var(--mint)", values: aligned(weeks, hit, (p) => p.rate) },
            {
              name: "committed + discarded",
              color: "var(--sage)",
              dashed: true,
              values: aligned(weeks, hit, (p) => p.rate_incl_discarded),
            },
          ]}
        />
        <ChartBars
          title="Adherence · days logged per week"
          bars={weeks.map((w) => {
            const days = adherenceBy.get(w) ?? 0;
            return {
              key: w,
              label: weekLabel(w),
              value: days,
              hint: `week of ${weekLabel(w)}: ${days}/7 days`,
            };
          })}
          yMax={7}
          target={data.baseline_days_per_week}
          targetLabel="Cronometer"
          color="var(--spruce-soft)"
          unit="days"
          showValues={weeks.length <= 14}
          labelEvery={Math.max(1, Math.ceil(weeks.length / 6))}
        />
      </section>

      <section className="exp-section">
        <h2 className="exp-h2">Secondary</h2>
        <div className="chart-small-grid">
          <ChartSmall title="Edit rate" values={aligned(weeks, sec, (p) => p.edit_rate)} format={pct} goal="down" />
          <ChartSmall
            title="Disambiguation"
            values={aligned(weeks, sec, (p) => p.disambiguation_rate)}
            format={pct}
            goal="down"
          />
          <ChartSmall
            title="Tier-3 runs"
            values={aligned(weeks, sec, (p) => p.tier3)}
            format={(v) => fmt0.format(v)}
            color="var(--amber)"
            goal="down"
          />
          <ChartSmall
            title="Short-circuit"
            values={aligned(weeks, sec, (p) => p.short_circuit_share)}
            format={pct}
            color="var(--mint)"
            goal="up"
          />
          <ChartSmall
            title="Discarded logs"
            values={aligned(weeks, sec, (p) => p.discarded_logs)}
            format={(v) => fmt0.format(v)}
            goal="down"
          />
          <ChartSmall
            title="Capture failures"
            values={aligned(weeks, sec, (p) => p.capture_failures)}
            format={(v) => fmt0.format(v)}
            color="var(--red)"
            goal="down"
          />
          <ChartSmall
            title="Committed logs"
            values={aligned(weeks, sec, (p) => p.committed_logs)}
            format={(v) => fmt0.format(v)}
          />
          <ChartSmall
            title="Items resolved"
            values={aligned(weeks, sec, (p) => p.items)}
            format={(v) => fmt0.format(v)}
          />
        </div>
      </section>

      <section className="exp-section">
        <h2 className="exp-h2">Duplicate-suspect food pairs · {dupes.length}</h2>
        {dupes.length === 0 ? (
          <p className="hhint">None — every food is stored once.</p>
        ) : (
          <ul className="exp-dupes">
            {dupes.slice(0, DUPES_SHOWN).map((d) => (
              <li key={`${d.food_a}-${d.food_b}`}>
                <span className="badge">{d.source}</span> {d.name_a}{" "}
                <span className="sep">≈</span> {d.name_b}
              </li>
            ))}
            {dupes.length > DUPES_SHOWN ? (
              <li className="more">+{dupes.length - DUPES_SHOWN} more</li>
            ) : null}
          </ul>
        )}
      </section>
    </>
  );
}
