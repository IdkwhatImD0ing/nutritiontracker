"use client";

import ChartBars, { type Bar } from "./ChartBars";
import ChartStrip, { type StripCell } from "./ChartStrip";
import {
  addDays,
  monthShort,
  parseISO,
  weekdayShort,
  type HistoryDay,
  type HistoryRange,
  type HistoryWeek,
  type RangeKind,
} from "./HistoryApi";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const MEAL_ORDER = ["breakfast", "lunch", "dinner", "snack"];
const MEAL_LETTER: Record<string, string> = {
  breakfast: "B",
  lunch: "L",
  dinner: "D",
  snack: "S",
};

type Props = {
  kind: RangeKind;
  range: HistoryRange;
  today: string;
  /** tap a day → its diary (§7.4) */
  onDay: (date: string) => void;
};

/** Pure renderer for the §7.4 History ranges — data in, taps out. The
 *  fetching shell lives in History.tsx; this split keeps the views
 *  renderable with fixed data. */
export default function HistoryView({ kind, range, today, onDay }: Props) {
  if (kind === "3d") {
    return <ThreeDays days={range.days} targets={range.targets} today={today} onDay={onDay} />;
  }
  if (kind === "y") {
    return <Year range={range} />;
  }
  return <Daily kind={kind} range={range} today={today} onDay={onDay} />;
}

// ---------------------------------------------------------------------------
// 3 days — per-day macro cards with full detail
// ---------------------------------------------------------------------------

function MacroBar({
  label,
  value,
  target,
  color,
  loud,
}: {
  label: string;
  value: number;
  target: number;
  color: string;
  loud?: boolean;
}) {
  const frac = target > 0 ? Math.min(value / target, 1) : 0;
  return (
    <div className={`hcard-macro${loud ? " loud" : ""}`}>
      <div className="hcard-macro-row">
        <span className="k">{label}</span>
        <span className="v num">
          {fmt.format(value)}g
          {target > 0 ? <span className="of"> / {fmt.format(target)}</span> : null}
        </span>
      </div>
      <div className="hcard-bar">
        <div className="fill" style={{ width: `${frac * 100}%`, background: color }} />
      </div>
    </div>
  );
}

function ThreeDays({
  days,
  targets,
  today,
  onDay,
}: {
  days: HistoryDay[];
  targets: HistoryRange["targets"];
  today: string;
  onDay: (date: string) => void;
}) {
  const ordered = [...days].reverse(); // newest on top
  return (
    <div className="hcards">
      {ordered.map((d) => {
        const date = parseISO(d.date);
        const kcalFrac = targets.kcal > 0 ? d.kcal / targets.kcal : 0;
        const mealChips = d.meals
          ? MEAL_ORDER.filter((m) => d.meals![m]).map((m) => ({
              meal: m,
              kcal: d.meals![m].kcal,
            }))
          : [];
        return (
          <button
            type="button"
            key={d.date}
            className={`hcard${d.logged ? "" : " empty"}`}
            onClick={() => onDay(d.date)}
            aria-label={`Open diary for ${d.date}`}
          >
            <div className="hcard-head">
              <span className="hcard-day">
                {d.date === today ? "Today" : weekdayShort(date)}
                <span className="hcard-date">
                  {" "}
                  {monthShort(date)} {date.getDate()}
                </span>
              </span>
              <span className="hcard-entries">
                {d.logged
                  ? `${d.entries} ${d.entries === 1 ? "entry" : "entries"}`
                  : "nothing logged"}
              </span>
              <span className="chev" aria-hidden="true" />
            </div>
            <div className="hcard-kcal num">
              <strong>{fmt.format(d.kcal)}</strong>
              {targets.kcal > 0 ? (
                <span className="of"> / {fmt.format(targets.kcal)} kcal</span>
              ) : (
                <span className="of"> kcal</span>
              )}
            </div>
            <div className="kcal-bar">
              <div
                className={`fill${kcalFrac > 1 ? " over" : ""}`}
                style={{ width: `${Math.min(kcalFrac, 1) * 100}%` }}
              />
            </div>
            <div className="hcard-macros">
              <MacroBar
                label="Protein"
                value={d.protein_g}
                target={targets.protein_g}
                color="var(--mint)"
                loud
              />
              <MacroBar label="Carbs" value={d.carb_g} target={targets.carb_g} color="var(--carb)" />
              <MacroBar label="Fat" value={d.fat_g} target={targets.fat_g} color="var(--fat)" />
            </div>
            {mealChips.length ? (
              <div className="hcard-meals num">
                {mealChips.map((c) => (
                  <span key={c.meal} title={c.meal}>
                    <span className="k">{MEAL_LETTER[c.meal] ?? c.meal}</span>{" "}
                    {fmt.format(c.kcal)}
                  </span>
                ))}
                {d.fiber_g != null ? (
                  <span title="fiber">
                    <span className="k">fiber</span> {fmt.format(d.fiber_g)}g
                  </span>
                ) : null}
              </div>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// week / month — daily kcal + protein bars against the target line
// ---------------------------------------------------------------------------

function Daily({
  kind,
  range,
  today,
  onDay,
}: {
  kind: "w" | "m";
  range: HistoryRange;
  today: string;
  onDay: (date: string) => void;
}) {
  const week = kind === "w";
  const toBar = (pick: (d: HistoryDay) => number, unit: string) =>
    range.days.map<Bar>((d) => {
      const date = parseISO(d.date);
      return {
        key: d.date,
        label: week
          ? weekdayShort(date)
          : date.getDate() === 1 || date.getDate() % 5 === 0
            ? `${monthShort(date)} ${date.getDate()}`
            : "",
        value: pick(d),
        muted: !d.logged,
        active: d.date === today,
        hint: `${weekdayShort(date)} ${monthShort(date)} ${date.getDate()}: ${fmt.format(pick(d))} ${unit}${
          d.logged ? "" : " (nothing logged)"
        }`,
      };
    });
  return (
    <div className="hcharts">
      <ChartBars
        title={week ? "Calories · last 7 days" : "Calories · last 30 days"}
        bars={toBar((d) => d.kcal, "kcal")}
        target={range.targets.kcal}
        color="var(--spruce-soft)"
        unit="kcal"
        showValues={week}
        labelEvery={1}
        onTap={onDay}
      />
      <ChartBars
        title={week ? "Protein · last 7 days" : "Protein · last 30 days"}
        bars={toBar((d) => d.protein_g, "g")}
        target={range.targets.protein_g}
        color="var(--mint)"
        unit="g"
        showValues={week}
        labelEvery={1}
        onTap={onDay}
        format={(v) => `${fmt.format(v)}g`}
      />
      <p className="hhint">Tap a day to open its diary.</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// year — weekly averages + adherence strip
// ---------------------------------------------------------------------------

function weekHint(w: HistoryWeek, v: number | null, unit: string): string {
  const s = parseISO(w.week_start);
  const e = addDays(s, 6);
  return `${monthShort(s)} ${s.getDate()} – ${monthShort(e)} ${e.getDate()}: ${
    v == null ? "—" : `${fmt.format(v)} ${unit} avg`
  } · ${w.days_logged}/7 days logged`;
}

function Year({ range }: { range: HistoryRange }) {
  const weeks = range.weeks;
  // a month label on the first week that starts in each new month
  let lastMonth = -1;
  const labels = weeks.map((w) => {
    const d = parseISO(w.week_start);
    if (d.getMonth() === lastMonth) return "";
    lastMonth = d.getMonth();
    return monthShort(d);
  });
  const bars = (pick: (w: HistoryWeek) => number | null, unit: string) =>
    weeks.map<Bar>((w, i) => ({
      key: w.week_start,
      label: labels[i],
      value: pick(w),
      muted: w.days_logged === 0,
      hint: weekHint(w, pick(w), unit),
    }));
  const cells = weeks.map<StripCell>((w, i) => ({
    key: w.week_start,
    hint: weekHint(w, w.avg?.kcal ?? null, "kcal"),
    level: w.adherence,
    label: labels[i],
  }));
  const loggedWeeks = weeks.filter((w) => w.days_logged > 0).length;
  const loggedDays = weeks.reduce((s, w) => s + w.days_logged, 0);
  const baseline = range.baseline_days_per_week;

  return (
    <div className="hcharts">
      <ChartBars
        title="Calories · weekly average, last 52 weeks"
        bars={bars((w) => w.avg?.kcal ?? null, "kcal")}
        target={range.targets.kcal}
        color="var(--spruce-soft)"
        unit="kcal"
        labelEvery={1}
      />
      <ChartBars
        title="Protein · weekly average"
        bars={bars((w) => w.avg?.protein_g ?? null, "g")}
        target={range.targets.protein_g}
        color="var(--mint)"
        unit="g"
        labelEvery={1}
        format={(v) => `${fmt.format(v)}g`}
      />
      <ChartStrip title="Days logged per week" cells={cells} />
      <p className="hhint">
        {loggedDays} days logged across {loggedWeeks} of {weeks.length} weeks
        {baseline > 0 ? ` · Cronometer baseline ${fmt.format(baseline)}/week` : ""}.
      </p>
    </div>
  );
}
