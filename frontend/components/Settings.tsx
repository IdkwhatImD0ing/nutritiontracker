"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  getSettings,
  putSettings,
  targetsEmpty,
  type Body,
  type MealWindows,
  type Settings as SettingsT,
  type Targets,
} from "./SettingsApi";
import { CM_PER_IN, LB_PER_KG } from "./WizardMath";
import WizardGoal from "./WizardGoal";
import AccountCard from "./AccountCard";
import OuraCard from "./OuraCard";
import ApiKeysCard from "./ApiKeysCard";
import Skeleton from "./Skeleton";
import { useFlash } from "./useFlash";

const WINDOW_MEALS = ["breakfast", "lunch", "dinner"] as const;
const MEALS = ["snack", "breakfast", "lunch", "dinner"];

const TARGET_FIELDS: { key: keyof Targets; label: string; unit: string }[] = [
  { key: "kcal", label: "Calories", unit: "kcal" },
  { key: "protein_g", label: "Protein", unit: "g" },
  { key: "carb_g", label: "Carbs", unit: "g" },
  { key: "fat_g", label: "Fat", unit: "g" },
];

type TargetText = Record<keyof Targets, string>;

function targetText(t: Partial<Targets>): TargetText {
  const s = (v: number | undefined) =>
    typeof v === "number" && v > 0 ? String(Math.round(v)) : "";
  return {
    kcal: s(t.kcal),
    protein_g: s(t.protein_g),
    carb_g: s(t.carb_g),
    fat_g: s(t.fat_g),
  };
}

function timezones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

/** "5′11″ · 180 lb · 34 · male" in the units the wizard was run in. */
function describeBody(b: Body): string | null {
  if (!b.weight_kg || !b.height_cm) return null;
  const parts: string[] = [];
  if (b.units === "imperial") {
    const totalIn = Math.round(b.height_cm / CM_PER_IN);
    parts.push(`${Math.floor(totalIn / 12)}′${totalIn % 12}″`);
    parts.push(`${Math.round(b.weight_kg * LB_PER_KG)} lb`);
  } else {
    parts.push(`${Math.round(b.height_cm)} cm`);
    parts.push(`${Math.round(b.weight_kg)} kg`);
  }
  if (b.age) parts.push(String(b.age));
  if (b.sex) parts.push(b.sex);
  return parts.join(" · ");
}

/** §7.4 Settings: targets, meal windows, timezone, profile_notes (the
 *  agent-visible free text, §6.1), the experiment link, the on-device-state
 *  note. One Save writes everything (PUT). The Cronometer baseline left this
 *  page on 2026-09-06 (owner feedback); the setting itself stays, read by the
 *  adherence chart, and PUT never touches a key it is not sent. */
export default function Settings() {
  const [form, setForm] = useState<SettingsT | null>(null);
  const [targets, setTargets] = useState<TargetText>(targetText({}));
  const [error, setError] = useState<string | null>(null);
  // the empty targets of a half-filled set, outlined until the next edit
  const [targetsInvalid, setTargetsInvalid] = useState(false);
  const [busy, setBusy] = useState(false);
  // "Saved." as a toast that clears itself, not a line that stays forever
  const [status, flashStatus] = useFlash(2500);
  const [wizard, setWizard] = useState(false);

  const load = (s: SettingsT) => {
    setForm(s);
    setTargets(targetText(s.targets));
  };

  // `attempt` re-runs the read: the failure notice carries a Retry rather
  // than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setError(null);
    getSettings()
      .then((s) => {
        if (cancelled) return;
        load(s);
        // Today's "Set them ›" (no targets yet) opens the wizard straight
        // away; the param goes, so a reload or Back doesn't reopen it.
        const params = new URLSearchParams(window.location.search);
        if (params.get("setup") === "targets") {
          params.delete("setup");
          const qs = params.toString();
          window.history.replaceState(
            null,
            "",
            `${window.location.pathname}${qs ? `?${qs}` : ""}`,
          );
          setWizard(true);
        }
      })
      .catch(() => {
        if (!cancelled) setError("Couldn't load your settings.");
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const setWindow = (
    meal: (typeof WINDOW_MEALS)[number],
    field: "start" | "end",
    value: string,
  ) =>
    setForm((f) =>
      f
        ? {
            ...f,
            meal_windows: {
              ...f.meal_windows,
              [meal]: { ...f.meal_windows[meal], [field]: value },
            } as MealWindows,
          }
        : f,
    );

  const save = async () => {
    if (!form) return;
    const parsed: Partial<Targets> = {};
    for (const f of TARGET_FIELDS) {
      const n = Number.parseFloat(targets[f.key]);
      if (Number.isFinite(n) && n > 0) parsed[f.key] = n;
    }
    // All four or none: a half-filled target set would make the tracker's
    // remaining-vs-target rings lie for the missing macros.
    const filled = TARGET_FIELDS.filter((f) => parsed[f.key] != null).length;
    if (filled !== 0 && filled !== TARGET_FIELDS.length) {
      setError("Fill in all four targets, or clear all four.");
      setTargetsInvalid(true);
      flashStatus(null);
      return;
    }
    setBusy(true);
    setError(null);
    flashStatus(null);
    try {
      load(
        await putSettings({
          targets: filled ? parsed : {},
          meal_windows: form.meal_windows,
          timezone: form.timezone.trim(),
          profile_notes: form.profile_notes,
        }),
      );
      flashStatus("Saved.");
    } catch {
      setError("Couldn't save — try again.");
    } finally {
      setBusy(false);
    }
  };

  if (wizard && form) {
    return (
      <WizardGoal
        initial={form.body}
        onDone={(s) => {
          load(s);
          setWizard(false);
          flashStatus("Targets saved.");
        }}
        onSkip={() => setWizard(false)}
      />
    );
  }

  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const body = form ? describeBody(form.body) : null;
  const noTargets = form != null && targetsEmpty(form);

  return (
    <main className="shell settings">
      <header className="brand">
        <h1>Settings</h1>
      </header>

      {error && !form ? (
        <div className="notice error" role="alert">
          <span>{error}</span>
          <button
            type="button"
            className="notice-btn"
            onClick={() => setAttempt((n) => n + 1)}
          >
            Retry
          </button>
        </div>
      ) : null}

      <AccountCard />
      <OuraCard />

      {!form ? (
        // Daily targets and Meal windows, roughly to size
        error ? null : (
          <>
            <Skeleton height={150} />
            <Skeleton height={220} />
          </>
        )
      ) : (
        <>
          <section className="m3-section" aria-label="Daily targets">
            <h2>Daily targets</h2>
            <div className="m3-card">
              {noTargets ? (
                <div className="notice warn">
                  <span>No daily targets yet. Today&rsquo;s rings need them.</span>
                  <button
                    type="button"
                    className="notice-btn"
                    onClick={() => setWizard(true)}
                  >
                    Set up
                  </button>
                </div>
              ) : null}
              <div className="m3-grid-4">
                {TARGET_FIELDS.map((f) => (
                  <label className="m3-field" key={f.key}>
                    {f.label}
                    <input
                      inputMode="decimal"
                      value={targets[f.key]}
                      onChange={(e) => {
                        setTargets((t) => ({ ...t, [f.key]: e.target.value }));
                        setTargetsInvalid(false);
                      }}
                      aria-invalid={
                        targetsInvalid && !(Number.parseFloat(targets[f.key]) > 0)
                      }
                      placeholder="—"
                      aria-label={`${f.label} target (${f.unit})`}
                      className="num"
                    />
                  </label>
                ))}
              </div>
              {/* with no targets, the notice above is the way in */}
              {noTargets && !body ? null : (
                <div className="m3-actions">
                  {noTargets ? null : (
                    <button
                      type="button"
                      className="m3-link-btn"
                      onClick={() => setWizard(true)}
                    >
                      Re-run the goal wizard
                    </button>
                  )}
                  {body ? <span className="m3-note">{body}</span> : null}
                </div>
              )}
            </div>
          </section>

          <section className="m3-section" aria-label="Meal windows">
            <h2>Meal windows</h2>
            <div className="m3-card">
              {WINDOW_MEALS.map((m) => (
                <div className="m3-window" key={m}>
                  <span>{m}</span>
                  <input
                    type="time"
                    value={form.meal_windows[m].start}
                    onChange={(e) => setWindow(m, "start", e.target.value)}
                    aria-label={`${m} start`}
                  />
                  <input
                    type="time"
                    value={form.meal_windows[m].end}
                    onChange={(e) => setWindow(m, "end", e.target.value)}
                    aria-label={`${m} end`}
                  />
                </div>
              ))}
              <div className="m3-window">
                <span>otherwise</span>
                <select
                  value={form.meal_windows.default}
                  onChange={(e) =>
                    setForm((f) =>
                      f
                        ? {
                            ...f,
                            meal_windows: { ...f.meal_windows, default: e.target.value },
                          }
                        : f,
                    )
                  }
                  aria-label="Meal outside every window"
                >
                  {MEALS.map((meal) => (
                    <option key={meal} value={meal}>
                      {meal}
                    </option>
                  ))}
                </select>
              </div>
              <p className="m3-note">
                Which meal a log lands in when you don&rsquo;t say — a spoken
                &ldquo;for lunch&rdquo; or &ldquo;this morning&rdquo; always wins.
              </p>
            </div>
          </section>

          <section className="m3-section" aria-label="Timezone">
            <h2>Timezone</h2>
            <label className="m3-field">
              IANA name
              <input
                list="usual-tz-list"
                value={form.timezone}
                onChange={(e) =>
                  setForm((f) => (f ? { ...f, timezone: e.target.value } : f))
                }
                autoComplete="off"
                spellCheck={false}
              />
            </label>
            <datalist id="usual-tz-list">
              {timezones().map((z) => (
                <option value={z} key={z} />
              ))}
            </datalist>
            {deviceTz && deviceTz !== form.timezone ? (
              <p className="m3-note">
                This device says {deviceTz}.{" "}
                <button
                  type="button"
                  className="m3-link-btn"
                  onClick={() =>
                    setForm((f) => (f ? { ...f, timezone: deviceTz } : f))
                  }
                >
                  Use it
                </button>
              </p>
            ) : null}
          </section>

          <section className="m3-section m3-wide" aria-label="Profile notes">
            <h2>Profile notes</h2>
            <label className="m3-field">
              What the assistant should always know
              <textarea
                value={form.profile_notes}
                onChange={(e) =>
                  setForm((f) => (f ? { ...f, profile_notes: e.target.value } : f))
                }
                placeholder={
                  "lactose intolerant; 'fairlife' always means Core Power Elite 42g; I train ~6pm; weekday lunch is the office cafeteria, its menu is at example.com"
                }
              />
            </label>
            <p className="m3-note">
              Injected verbatim every time a log is parsed, a food is searched
              for, or a plate is photographed. Preferences, conventions, what a
              word means to you — and where you usually eat: a menu named here
              is one the photo lane can go read.
            </p>
          </section>

          {/* by the button that was tapped, not under the title far above */}
          {error ? (
            <p className="sheet-error" role="alert">
              {error}
            </p>
          ) : null}
          <button
            type="button"
            className="commit-btn"
            disabled={busy}
            aria-busy={busy}
            onClick={save}
          >
            {busy ? (
              <>
                <span className="spinner on-accent" />
                Saving…
              </>
            ) : (
              "Save settings"
            )}
          </button>
          {status ? (
            <div className="toast" role="status">
              <span>{status}</span>
            </div>
          ) : null}
        </>
      )}

      {/* Neither needs the settings read, so a failed one doesn't hide them. */}
      <ApiKeysCard />

      <section className="m3-section" aria-label="More">
        <div className="m3-card">
          <div className="m3-row">
            <span className="k">Is it learning?</span>
            <Link href="/experiment" className="m3-link-btn">
              Experiment charts →
            </Link>
          </div>
          <p className="m3-note">
            Deleting the home-screen icon clears the app&rsquo;s on-device
            state (your sign-in, any late items still sending). Your diary
            lives on the server and is untouched.
          </p>
          <p className="m3-note">Data: USDA FoodData Central · Open Food Facts</p>
        </div>
      </section>
    </main>
  );
}
