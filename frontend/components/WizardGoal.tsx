"use client";

import { useEffect, useRef, useState } from "react";
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
import { useSheetBack } from "./sheetBack";
import { useSheetFocus } from "./useSheetFocus";
import "@/app/library/m3.css";

type Props = {
  /** settings.body from a previous run — pre-fills every field */
  initial?: Body | null;
  onDone: (s: Settings) => void;
  /** first run only: "Skip for now" on step 1 */
  onSkip?: () => void;
  /** a re-run from Settings: Cancel in the top bar on every step (and
   *  Escape and system Back) leaves without saving, in place of Skip */
  onCancel?: () => void;
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
export default function WizardGoal({ initial, onDone, onSkip, onCancel }: Props) {
  const sheetRef = useSheetFocus<HTMLDivElement>();
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
  // Why Next / Compute stays disabled, once every field has something in it
  // (a blank field explains itself): the first number out of range, in the
  // units shown. 67–661 lb and 3 ft 4 in – 8 ft 2 in are the whole numbers
  // inside 30–300 kg and 100–250 cm.
  const weightRange = units === "metric" ? "30–300 kg" : "67–661 lb";
  const bodyFilled =
    age.trim() !== "" &&
    weight.trim() !== "" &&
    (units === "metric" ? heightCm : feet).trim() !== "";
  const bodyProblem =
    !bodyFilled || bodyValid
      ? null
      : !(ageN >= 13 && ageN <= 120)
        ? "Age must be 13–120."
        : !(kg >= 30 && kg <= 300)
          ? `Weight must be ${weightRange}.`
          : `Height must be ${units === "metric" ? "100–250 cm" : "3 ft 4 in – 8 ft 2 in"}.`;
  const goalProblem =
    target.trim() !== "" && !goalValid ? `Target weight must be ${weightRange}.` : null;

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

  // A re-run closes on Back and Escape as any sheet does (sheetBack). Not
  // while a request is out, Cancel included: a save can't be called back,
  // and its "Targets saved." would land after a wizard that seemed cancelled.
  useSheetBack(
    sheetRef,
    () => {
      if (!busy) onCancel?.();
    },
    !!onCancel,
  );

  // It covers the whole screen, so what it covers is inert while it is up:
  // Tab and a screen reader stay out of the page and the tab bar beside it
  // (Settings' <main> is inert by its own prop, and left to it).
  useEffect(() => {
    const el = sheetRef.current;
    const covered = [...(el?.parentElement?.children ?? [])].filter(
      (c): c is HTMLElement => c !== el && c instanceof HTMLElement && !c.inert,
    );
    for (const c of covered) c.inert = true;
    return () => {
      for (const c of covered) c.inert = false;
    };
  }, [sheetRef]);

  // A step's buttons leave with it, and focus would fall out of the dialog
  // to <body>: the next step's title takes it, so a screen reader starts
  // there.
  const title = useRef<HTMLHeadingElement>(null);
  const shownStep = useRef(step);
  useEffect(() => {
    if (shownStep.current === step) return;
    shownStep.current = step;
    title.current?.focus({ preventScroll: true });
  }, [step]);

  return (
    <div
      ref={sheetRef}
      className="wizard"
      role="dialog"
      aria-modal="true"
      aria-label="Goal wizard"
      tabIndex={-1}
    >
      <div className="wizard-inner">
        <div className="top-bar wizard-top">
          {onCancel ? (
            <button
              type="button"
              className="wizard-cancel"
              disabled={busy}
              onClick={onCancel}
            >
              Cancel
            </button>
          ) : null}
          <div className="wizard-steps" aria-hidden="true">
            {[0, 1, 2].map((i) => (
              <span key={i} className={i <= step ? "done" : undefined} />
            ))}
          </div>
        </div>

        {step === 0 ? (
          <>
            <h1 ref={title} tabIndex={-1}>
              About you
            </h1>
            <p className="wizard-lead">
              Four numbers and a rough activity level — enough to estimate what
              you burn in a day. Every target you get is editable.
            </p>

            <div className="m3-seg" role="group" aria-label="Units">
              {(["imperial", "metric"] as Units[]).map((u) => (
                <button
                  key={u}
                  type="button"
                  className={units === u ? "active" : undefined}
                  aria-pressed={units === u}
                  onClick={() => switchUnits(u)}
                >
                  {u === "imperial" ? "lb · ft" : "kg · cm"}
                </button>
              ))}
            </div>

            <div className="m3-seg" role="group" aria-label="Sex">
              {(["male", "female"] as Sex[]).map((s) => (
                <button
                  key={s}
                  type="button"
                  className={sex === s ? "active" : undefined}
                  aria-pressed={sex === s}
                  onClick={() => setSex(s)}
                >
                  {s === "male" ? "Male" : "Female"}
                </button>
              ))}
            </div>

            {/* Each number carries its unit inside the box. A unit, not an
                example: a grey "180" in an empty field reads as data already
                filled in. The visible unit is aria-hidden; the label or
                aria-label says it for a screen reader. */}
            <div className="m3-grid-2">
              <label className="m3-field">
                Age
                <span className="m3-unit-wrap">
                  <input
                    inputMode="numeric"
                    enterKeyHint="next"
                    value={age}
                    onChange={(e) => setAge(e.target.value)}
                    className="num"
                  />
                  <span className="m3-unit" aria-hidden="true">
                    yrs
                  </span>
                </span>
              </label>
              <label className="m3-field">
                Weight
                <span className="m3-unit-wrap">
                  <input
                    inputMode="decimal"
                    enterKeyHint="next"
                    value={weight}
                    onChange={(e) => setWeight(e.target.value)}
                    aria-label={`Weight (${wUnit})`}
                    className="num"
                  />
                  <span className="m3-unit" aria-hidden="true">
                    {wUnit}
                  </span>
                </span>
              </label>
            </div>

            {units === "metric" ? (
              <label className="m3-field">
                Height
                <span className="m3-unit-wrap">
                  <input
                    inputMode="decimal"
                    enterKeyHint="next"
                    value={heightCm}
                    onChange={(e) => setHeightCm(e.target.value)}
                    aria-label="Height (cm)"
                    className="num"
                  />
                  <span className="m3-unit" aria-hidden="true">
                    cm
                  </span>
                </span>
              </label>
            ) : (
              // one Height caption over its two boxes (the inches box used to
              // be a field of its own labelled "(in)"); a group, so a screen
              // reader hears "Height" before "feet" and "inches"
              <div className="m3-field" role="group" aria-labelledby="wizard-height">
                <span id="wizard-height">Height</span>
                <div className="m3-grid-2">
                  <span className="m3-unit-wrap">
                    <input
                      inputMode="numeric"
                      enterKeyHint="next"
                      value={feet}
                      onChange={(e) => setFeet(e.target.value)}
                      aria-label="Height, feet"
                      className="num"
                    />
                    <span className="m3-unit" aria-hidden="true">
                      ft
                    </span>
                  </span>
                  <span className="m3-unit-wrap">
                    <input
                      inputMode="numeric"
                      enterKeyHint="next"
                      value={inches}
                      onChange={(e) => setInches(e.target.value)}
                      aria-label="Height, inches"
                      className="num"
                    />
                    <span className="m3-unit" aria-hidden="true">
                      in
                    </span>
                  </span>
                </div>
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

            {/* kept mounted, so the reason Next is off is announced */}
            <p className="m3-note" role="status">
              {bodyProblem}
            </p>

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!bodyValid}
                onClick={() => setStep(1)}
              >
                Next
              </button>
              {onSkip && !onCancel ? (
                <button type="button" className="wizard-ghost" onClick={onSkip}>
                  Skip for now
                </button>
              ) : null}
            </div>
          </>
        ) : null}

        {step === 1 ? (
          <>
            <h1 ref={title} tabIndex={-1}>
              Your goal
            </h1>
            <p className="wizard-lead">
              Where you want to be and how fast. Slower is easier to keep up —
              and keeps more muscle.
            </p>

            <label className="m3-field">
              Target weight
              <span className="m3-unit-wrap">
                <input
                  inputMode="decimal"
                  enterKeyHint="done"
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                  aria-label={`Target weight (${wUnit})`}
                  className="num"
                />
                <span className="m3-unit" aria-hidden="true">
                  {wUnit}
                </span>
              </span>
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
            <p className="m3-note" role="status">
              {goalProblem}
            </p>

            {error ? (
              <p className="sheet-error" role="alert">
                {error}
              </p>
            ) : null}

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!goalValid || busy}
                aria-busy={busy}
                onClick={compute}
              >
                {busy ? (
                  <>
                    <span className="spinner on-accent" />
                    Computing…
                  </>
                ) : (
                  "See my targets"
                )}
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
            <h1 ref={title} tabIndex={-1}>
              Your targets
            </h1>
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
                  ["kcal", "Calories", "kcal"],
                  ["protein_g", "Protein", "g"],
                  ["carb_g", "Carbs", "g"],
                  ["fat_g", "Fat", "g"],
                ] as [keyof Targets, string, string][]
              ).map(([k, label, unit]) => (
                <label className="m3-field" key={k}>
                  {label}
                  <span className="m3-unit-wrap">
                    <input
                      inputMode="decimal"
                      value={edit[k]}
                      onChange={(e) => setEdit((t) => ({ ...t, [k]: e.target.value }))}
                      aria-label={`${label} target (${unit})`}
                      className="num"
                    />
                    <span className="m3-unit" aria-hidden="true">
                      {unit}
                    </span>
                  </span>
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
            <p className="m3-note" role="status">
              {editedValid
                ? null
                : "Calories must be 800–10,000; protein and fat 0–1,000 g, carbs 0–2,000 g."}
            </p>

            {error ? (
              <p className="sheet-error" role="alert">
                {error}
              </p>
            ) : null}

            <div className="wizard-foot">
              <button
                type="button"
                className="commit-btn"
                disabled={!editedValid || busy}
                aria-busy={busy}
                onClick={save}
              >
                {busy ? (
                  <>
                    <span className="spinner on-accent" />
                    Saving…
                  </>
                ) : (
                  "Save targets"
                )}
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
