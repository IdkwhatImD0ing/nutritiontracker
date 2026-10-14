"use client";

import { useState } from "react";
import { ApiError } from "@/lib/api";
import {
  computeGoals,
  putSettings,
  type Body,
  type Settings,
  type Targets,
} from "./SettingsApi";
import {
  ACTIVITY,
  CM_PER_IN,
  LB_PER_KG,
  isGoalResult,
  type Activity,
  type GoalInput,
  type GoalResult,
  type Sex,
} from "./WizardMath";
import "@/app/library/m3.css";

type Props = {
  /** settings.body from a previous run — pre-fills every field */
  initial?: Body | null;
  onDone: (s: Settings) => void;
  onSkip?: () => void;
};

type Units = "metric" | "imperial";

const RATES: { kg: number; imperial: string; metric: string }[] = [
  { kg: -0.9, imperial: "Lose 2 lb a week", metric: "Lose 0.9 kg a week" },
  { kg: -0.45, imperial: "Lose 1 lb a week", metric: "Lose 0.45 kg a week" },
  { kg: -0.23, imperial: "Lose ½ lb a week", metric: "Lose 0.25 kg a week" },
  { kg: 0, imperial: "Maintain", metric: "Maintain" },
  { kg: 0.23, imperial: "Gain ½ lb a week", metric: "Gain 0.25 kg a week" },
  { kg: 0.45, imperial: "Gain 1 lb a week", metric: "Gain 0.45 kg a week" },
];

/** routes/settings.py TargetsIn — matched exactly so a bad number is caught
 *  here instead of coming back as a 422. */
const LIMITS: Record<keyof Targets, [number, number]> = {
  kcal: [800, 10000],
  protein_g: [0, 1000],
  carb_g: [0, 2000],
  fat_g: [0, 1000],
};

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** routes/settings.py field names (the last `loc` segment of a 422) → what
 *  the form calls them. */
const FIELD_LABEL: Record<string, string> = {
  height_cm: "height",
  weight_kg: "weight",
  age: "age",
  sex: "sex",
  activity_level: "activity level",
  activity: "activity level",
  goal_weight_kg: "target weight",
  target_weight_kg: "target weight",
  weekly_rate_kg: "weekly rate",
  kcal: "calories",
  protein_g: "protein",
  carb_g: "carbs",
  fat_g: "fat",
  timezone: "time zone",
};

/** A 4xx says what is wrong — show that, not "check your connection".
 *  FastAPI puts it in `detail`: a string (ApiError has parsed it), or a 422
 *  list of {loc, msg}. pydantic's msg is in kg/cm whatever units are picked,
 *  so a 422 names the field instead. Anything else (an HTML 404 page, or
 *  FastAPI's own "Not Found" / "Method Not Allowed" for a misroute — these
 *  routes raise no 404 or 405 of their own) is null, and the caller's own
 *  sentence shows. */
function detailOf(e: unknown): string | null {
  if (!(e instanceof ApiError) || e.status < 400 || e.status >= 500) return null;
  if (e.detail && e.status !== 404 && e.status !== 405) return e.detail;
  const body = e.message.slice(e.message.indexOf(":") + 1).trim();
  let fields: string[] = [];
  try {
    const d = (JSON.parse(body) as { detail?: unknown }).detail;
    if (Array.isArray(d)) {
      fields = d.map((x) => String((x as { loc?: unknown[] }).loc?.at(-1) ?? ""));
    }
  } catch {
    // ApiError caps the body at 200 chars, so a long 422 arrives truncated
    fields = [...body.matchAll(/"loc":\[([^\]]*)\]/g)].map((m) =>
      (m[1].split(",").at(-1) ?? "").replace(/"/g, ""),
    );
  }
  const labels = [...new Set(fields.map((f) => FIELD_LABEL[f]).filter(Boolean))];
  return labels.length ? `Check your ${labels.join(" and ")}.` : null;
}

function num(s: string): number {
  const n = Number.parseFloat(s);
  return Number.isFinite(n) ? n : NaN;
}

function str(n: number | undefined, digits = 0): string {
  return typeof n === "number" && Number.isFinite(n) ? n.toFixed(digits) : "";
}

function nearestRate(kg: number | undefined): number {
  if (typeof kg !== "number") return 0;
  return RATES.reduce((best, r) =>
    Math.abs(r.kg - kg) < Math.abs(best - kg) ? r.kg : best, RATES[0].kg);
}

/** §7.4 first-run goal wizard (Cronometer-style): body → goal → computed
 *  kcal + macro targets, every number shown and editable before saving to
 *  settings. Re-runnable from Settings any time. */
export default function WizardGoal({ initial, onDone, onSkip }: Props) {
  const init = initial ?? {};
  const [step, setStep] = useState(0);
  const [units, setUnits] = useState<Units>(init.units ?? "imperial");
  const [sex, setSex] = useState<Sex>(init.sex ?? "male");
  const [age, setAge] = useState(str(init.age));
  const [heightCm, setHeightCm] = useState(str(init.height_cm));
  const [feet, setFeet] = useState(
    init.height_cm ? String(Math.floor(init.height_cm / CM_PER_IN / 12)) : "",
  );
  const [inches, setInches] = useState(
    init.height_cm ? String(Math.round(init.height_cm / CM_PER_IN) % 12) : "",
  );
  const [weight, setWeight] = useState(
    init.weight_kg
      ? str(init.units === "metric" ? init.weight_kg : init.weight_kg * LB_PER_KG, 1)
      : "",
  );
  const [activity, setActivity] = useState<Activity>(init.activity ?? "moderate");
  const [target, setTarget] = useState(
    init.target_weight_kg
      ? str(
          init.units === "metric"
            ? init.target_weight_kg
            : init.target_weight_kg * LB_PER_KG,
          1,
        )
      : "",
  );
  const [rateKg, setRateKg] = useState(nearestRate(init.weekly_rate_kg));

  const [result, setResult] = useState<GoalResult | null>(null);
  const [edit, setEdit] = useState<Record<keyof Targets, string>>({
    kcal: "",
    protein_g: "",
    carb_g: "",
    fat_g: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // derived metric values (the formula and the stored body are metric)
  const kg = units === "metric" ? num(weight) : num(weight) / LB_PER_KG;
  const cm =
    units === "metric"
      ? num(heightCm)
      : (num(feet) * 12 + (inches.trim() ? num(inches) : 0)) * CM_PER_IN;
  const targetKg = units === "metric" ? num(target) : num(target) / LB_PER_KG;
  const ageN = num(age);

  // bounds are routes/settings.py BodyProfile / GoalInputs, to the number
  const bodyValid =
    ageN >= 13 && ageN <= 120 && cm >= 100 && cm <= 250 && kg >= 30 && kg <= 300;
  const goalValid = targetKg >= 30 && targetKg <= 300;

  // The server derives the direction from goal weight vs current weight and
  // takes the rate as a magnitude, so only rates that agree with the target
  // are offerable — otherwise "Lose" could silently compute a surplus.
  const dir = goalValid && Number.isFinite(kg) ? Math.sign(targetKg - kg) : 0;
  const rate = Math.sign(rateKg) === dir ? rateKg : 0;

  const switchUnits = (u: Units) => {
    if (u === units) return;
    // convert whatever is typed so nothing is lost on the toggle
    if (u === "metric") {
      if (Number.isFinite(cm)) setHeightCm(str(cm));
      if (Number.isFinite(kg)) setWeight(str(kg, 1));
      if (Number.isFinite(targetKg)) setTarget(str(targetKg, 1));
    } else {
      if (Number.isFinite(cm)) {
        const totalIn = Math.round(cm / CM_PER_IN);
        setFeet(String(Math.floor(totalIn / 12)));
        setInches(String(totalIn % 12));
      }
      if (Number.isFinite(kg)) setWeight(str(kg * LB_PER_KG, 1));
      if (Number.isFinite(targetKg)) setTarget(str(targetKg * LB_PER_KG, 1));
    }
    setUnits(u);
  };

  const input = (): GoalInput => ({
    sex,
    age: Math.round(ageN),
    height_cm: Math.round(cm * 10) / 10,
    weight_kg: Math.round(kg * 10) / 10,
    activity,
    target_weight_kg: Math.round(targetKg * 10) / 10,
    weekly_rate_kg: rate,
  });

  const compute = async () => {
    setBusy(true);
    setError(null);
    try {
      // §7.4 formula lives server-side only (services/goals.py) — one copy of
      // the math, so what is previewed is what gets saved.
      const r: unknown = await computeGoals(input());
      if (!isGoalResult(r)) throw new Error("unexpected response");
      setResult(r);
      setEdit({
        kcal: String(r.targets.kcal),
        protein_g: String(r.targets.protein_g),
        carb_g: String(r.targets.carb_g),
        fat_g: String(r.targets.fat_g),
      });
      setStep(2);
    } catch (e) {
      setError(
        detailOf(e) ?? "Couldn't compute targets — check your connection.",
      );
    }
    setBusy(false);
  };

  const edited: Partial<Targets> = {
    kcal: num(edit.kcal),
    protein_g: num(edit.protein_g),
    carb_g: num(edit.carb_g),
    fat_g: num(edit.fat_g),
  };
  const editedValid = (Object.keys(LIMITS) as (keyof Targets)[]).every((k) => {
    const v = edited[k];
    return v != null && v >= LIMITS[k][0] && v <= LIMITS[k][1];
  });
  const macroKcal =
    (edited.protein_g ?? 0) * 4 + (edited.carb_g ?? 0) * 4 + (edited.fat_g ?? 0) * 9;
  const macroOff =
    Number.isFinite(edited.kcal ?? NaN) && Math.abs(macroKcal - (edited.kcal ?? 0)) > 50;

  const save = async () => {
    if (!editedValid) return;
    setBusy(true);
    setError(null);
    try {
      const s = await putSettings({
        body: { ...input(), units },
        targets: edited as Targets,
      });
      onDone(s);
    } catch (e) {
      setError(detailOf(e) ?? "Couldn't save — check your connection and try again.");
      setBusy(false);
    }
  };

  const weeksToTarget =
    rate !== 0 && Number.isFinite(targetKg) && Number.isFinite(kg)
      ? Math.abs(targetKg - kg) / Math.abs(rate)
      : null;
  const wUnit = units === "metric" ? "kg" : "lb";

  return (
    <div className="wizard" role="dialog" aria-label="Goal wizard">
      <div className="wizard-inner">
        <div className="wizard-steps" aria-hidden="true">
          {[0, 1, 2].map((i) => (
            <span key={i} className={i <= step ? "done" : undefined} />
          ))}
        </div>

        {step === 0 ? (
          <>
            <h1>About you</h1>
            <p className="wizard-lead">
              Four numbers and a rough activity level — enough to estimate what
              you burn in a day. Every target you get is editable.
            </p>

            <div className="m3-seg" role="radiogroup" aria-label="Units">
              {(["imperial", "metric"] as Units[]).map((u) => (
                <button
                  key={u}
                  type="button"
                  className={units === u ? "active" : undefined}
                  onClick={() => switchUnits(u)}
                >
                  {u === "imperial" ? "lb · ft" : "kg · cm"}
                </button>
              ))}
            </div>

            <div className="m3-seg" role="radiogroup" aria-label="Sex">
              {(["male", "female"] as Sex[]).map((s) => (
                <button
                  key={s}
                  type="button"
                  className={sex === s ? "active" : undefined}
                  onClick={() => setSex(s)}
                >
                  {s === "male" ? "Male" : "Female"}
                </button>
              ))}
            </div>

            <div className="m3-grid-2">
              <label className="m3-field">
                Age
                <input
                  inputMode="numeric"
                  value={age}
                  onChange={(e) => setAge(e.target.value)}
                  className="num"
                />
              </label>
              <label className="m3-field">
                Weight ({wUnit})
                <input
                  inputMode="decimal"
                  value={weight}
                  onChange={(e) => setWeight(e.target.value)}
                  className="num"
                />
              </label>
            </div>

            {units === "metric" ? (
              <label className="m3-field">
                Height (cm)
                <input
                  inputMode="decimal"
                  value={heightCm}
                  onChange={(e) => setHeightCm(e.target.value)}
                  className="num"
                />
              </label>
            ) : (
              <div className="m3-grid-2">
                <label className="m3-field">
                  Height (ft)
                  <input
                    inputMode="numeric"
                    value={feet}
                    onChange={(e) => setFeet(e.target.value)}
                    className="num"
                  />
                </label>
                <label className="m3-field">
                  (in)
                  <input
                    inputMode="numeric"
                    value={inches}
                    onChange={(e) => setInches(e.target.value)}
                    className="num"
                  />
                </label>
              </div>
            )}

            <label className="m3-field">
              Activity
              <select
                value={activity}
                onChange={(e) => setActivity(e.target.value as Activity)}
              >
                {ACTIVITY.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label}
                  </option>
                ))}
              </select>
            </label>

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!bodyValid}
                onClick={() => setStep(1)}
              >
                Next
              </button>
              {onSkip ? (
                <button type="button" className="wizard-ghost" onClick={onSkip}>
                  Skip for now
                </button>
              ) : null}
            </div>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <h1>Your goal</h1>
            <p className="wizard-lead">
              Where you want to be and how fast. Slower is easier to keep up —
              and keeps more muscle.
            </p>

            <label className="m3-field">
              Target weight ({wUnit})
              <input
                inputMode="decimal"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
                className="num"
              />
            </label>

            <label className="m3-field">
              Weekly rate
              <select
                value={rate}
                onChange={(e) => setRateKg(Number(e.target.value))}
              >
                {RATES.map((r) => (
                  <option
                    key={r.kg}
                    value={r.kg}
                    disabled={Math.sign(r.kg) !== dir && r.kg !== 0}
                  >
                    {r[units]}
                  </option>
                ))}
              </select>
            </label>

            {goalValid && dir === 0 ? (
              <p className="m3-note">
                Target matches your current weight — maintenance.
              </p>
            ) : null}
            {goalValid && weeksToTarget != null && weeksToTarget > 0 ? (
              <p className="m3-note num">
                About {fmt.format(weeksToTarget)} week
                {Math.round(weeksToTarget) === 1 ? "" : "s"} at this rate.
              </p>
            ) : null}

            {error ? <p className="sheet-error">{error}</p> : null}

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!goalValid || busy}
                onClick={compute}
              >
                {busy ? "Computing…" : "See my targets"}
              </button>
              <button
                type="button"
                className="wizard-ghost"
                onClick={() => setStep(0)}
              >
                Back
              </button>
            </div>
          </>
        ) : null}

        {step === 2 && result ? (
          <>
            <h1>Your targets</h1>
            <p className="wizard-lead">
              Your resting burn (Mifflin-St Jeor) times your activity, minus
              your weekly rate. Protein scales with bodyweight, fat has a floor,
              carbs fill the rest. Change anything.
            </p>

            <div className="wizard-hero num">
              <div className="big">
                {/* "2,854" like every other figure; the field below keeps what was typed */}
                {edited.kcal != null && Number.isFinite(edited.kcal) ? fmt.format(edited.kcal) : "—"}
                <small>kcal / day</small>
              </div>
              <div className="sub">
                Resting {fmt.format(result.bmr)} · with activity{" "}
                {fmt.format(result.tdee)}
                {rate !== 0
                  ? ` · ${rate < 0 ? "−" : "+"}${fmt.format(Math.abs(result.tdee - result.targets.kcal))} for the goal`
                  : ""}
              </div>
            </div>

            {/* services/goals.py writes each note as a sentence */}
            {result.notes?.map((n) => (
              <p className="m3-note warn" key={n}>
                {n}
              </p>
            ))}

            <div className="m3-grid-4">
              {(
                [
                  ["kcal", "Calories"],
                  ["protein_g", "Protein"],
                  ["carb_g", "Carbs"],
                  ["fat_g", "Fat"],
                ] as [keyof Targets, string][]
              ).map(([k, label]) => (
                <label className="m3-field" key={k}>
                  {label}
                  <input
                    inputMode="decimal"
                    value={edit[k]}
                    onChange={(e) => setEdit((t) => ({ ...t, [k]: e.target.value }))}
                    className="num"
                  />
                </label>
              ))}
            </div>
            <p className={`m3-note num${macroOff ? " warn" : ""}`}>
              Macros add up to {fmt.format(macroKcal)} kcal
              {macroOff
                ? ` — ${fmt.format(Math.abs(macroKcal - (edited.kcal ?? 0)))} ${
                    macroKcal > (edited.kcal ?? 0) ? "over" : "under"
                  } the calorie target`
                : ""}
              .
            </p>
            {!editedValid ? (
              <p className="m3-note">
                Calories must be 800–10,000; protein and fat 0–1,000 g, carbs
                0–2,000 g.
              </p>
            ) : null}

            {error ? <p className="sheet-error">{error}</p> : null}

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!editedValid || busy}
                onClick={save}
              >
                {busy ? "Saving…" : "Save targets"}
              </button>
              <button
                type="button"
                className="wizard-ghost"
                disabled={busy}
                onClick={() => {
                  setError(null);
                  setStep(1);
                }}
              >
                Back
              </button>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
