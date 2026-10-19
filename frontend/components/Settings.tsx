"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { DayTotals } from "@/lib/api";
import { mealLabel } from "@/lib/labels";
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
import { useMe } from "./MeApi";
import { isoDate } from "./HistoryApi";
import { dayKey, peek, put } from "./readCache";
import { useAutoGrow } from "./useAutoGrow";
import { useFlash } from "./useFlash";
import { useScrollMemory } from "./useScrollMemory";
import Skeleton from "./Skeleton";

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

/** Today paints the diary it last showed (readCache) before its own read
 *  lands, and works the tracker's "left" out from that copy's targets: new
 *  targets go into it, or a return to Today shows the old ones first. */
function putTodayTargets(targets: Partial<Targets>) {
  const key = dayKey(isoDate(new Date()));
  const day = peek<{ totals: DayTotals }>(key);
  if (day) put(key, { ...day, totals: { ...day.totals, targets } });
}

function timezones(): string[] {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
}

/** "Pacific Time" for America/Los_Angeles; null where the browser has none. */
function zoneGeneric(zone: string): string | null {
  try {
    return (
      new Intl.DateTimeFormat("en-US", { timeZone: zone, timeZoneName: "longGeneric" })
        .formatToParts(new Date())
        .find((p) => p.type === "timeZoneName")?.value ?? null
    );
  } catch {
    return null; // not a zone this browser knows
  }
}

/** "Los Angeles — Pacific Time"; a zone inside a sub-region reads city
 *  first ("Knox, Indiana — Central Time"). A zone with no region ("UTC",
 *  the server's default) is its own name. The id itself is the value. */
function zoneLabel(zone: string): string {
  if (!zone.includes("/")) return zone;
  const place = zone.split("/").slice(1).reverse().join(", ").replace(/_/g, " ");
  const generic = zoneGeneric(zone);
  return generic ? `${place} — ${generic}` : place;
}

type ZoneOption = { id: string; label: string };

/** Every zone the browser knows, grouped by IANA region (America, Europe…)
 *  for <optgroup>s and sorted by label within each. Zones without a region
 *  ("UTC") go under Other. */
function zoneGroups(): [string, ZoneOption[]][] {
  const groups = new Map<string, ZoneOption[]>();
  for (const id of timezones()) {
    const region = id.includes("/") ? id.slice(0, id.indexOf("/")) : "Other";
    const list = groups.get(region) ?? [];
    list.push({ id, label: zoneLabel(id) });
    groups.set(region, list);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.label.localeCompare(b.label));
  }
  return [...groups];
}

type ZoneList = { groups: [string, ZoneOption[]][]; ids: Set<string> };
let zoneList: ZoneList | null = null;

/** zoneGroups() and its ids, built once per document: about 420 labels,
 *  each from its own formatter, are a long task, and Settings mounts afresh
 *  on every tab switch. */
function zoneOptions(): ZoneList {
  if (!zoneList) {
    const groups = zoneGroups();
    zoneList = { groups, ids: new Set(groups.flatMap(([, l]) => l.map((z) => z.id))) };
  }
  return zoneList;
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
 *  agent-visible free text, §6.1), then an About group: rows to the
 *  experiment charts (owner only), the API-key docs and the legal pages
 *  over the on-device-state note and the data credits. One Save writes
 *  everything (PUT); it and Discard show in a bar above the tab bar only
 *  while the form differs from what was last read or saved. The Cronometer
 *  baseline left this page on 2026-09-06 (owner feedback); the setting
 *  itself stays, read by the adherence chart, and PUT never touches a key
 *  it is not sent. */
export default function Settings() {
  // A return to the tab paints the settings last read (readCache: the
  // server's copy, never an unsaved edit) while the read below refreshes them.
  const [form, setForm] = useState<SettingsT | null>(() => peek<SettingsT>("settings"));
  // the settings as last read or written: what Discard puts back, and what
  // the form is compared with to know whether anything is unsaved. The
  // cached copy is the server's, so it starts as both.
  const [saved, setSaved] = useState<SettingsT | null>(form);
  const [targets, setTargets] = useState<TargetText>(() =>
    targetText(form?.targets ?? {}),
  );
  // what the page opened with, so the read below can tell an edit begun on
  // the cached form (kept) from the form as painted (refreshed)
  const opened = useRef({ form, targets }).current;
  const [error, setError] = useState<string | null>(null);
  // the empty targets of a half-filled set, outlined until the next edit
  const [targetsInvalid, setTargetsInvalid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [status, flashStatus] = useFlash(3000);
  const [wizard, setWizard] = useState(false);
  const me = useMe();
  // The notes box grows with the note instead of scrolling inside the page.
  // The wizard is an overlay, so the box stays mounted (and measured) under it.
  const notesRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(notesRef, form?.profile_notes ?? "");
  const zones = zoneOptions();
  // writes that have landed (Save, the wizard): a read sent before one of
  // them answers with older settings than the page now shows
  const writes = useRef(0);

  // the server's answer to a save, or Discard putting the last one back
  const load = (s: SettingsT) => {
    setForm(s);
    setSaved(s);
    setTargets(targetText(s.targets));
    put("settings", s);
    putTodayTargets(s.targets);
  };

  /** After any edit a "Saved." is stale, and so is a refused save's reason. */
  const edited = () => {
    setError(null);
    flashStatus(null);
  };

  const edit = (change: (f: SettingsT) => SettingsT) => {
    setForm((f) => (f ? change(f) : f));
    edited();
  };

  // `attempt` re-runs the read: the failure notice carries a Retry rather
  // than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const sent = writes.current;
    setError(null);
    getSettings()
      .then((s) => {
        if (cancelled) return;
        if (writes.current === sent) {
          setForm((f) => (f === opened.form ? s : f));
          setSaved(s);
          setTargets((t) => (t === opened.targets ? targetText(s.targets) : t));
          put("settings", s);
        }
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
        if (cancelled || writes.current !== sent) return;
        setError(
          opened.form
            ? "Couldn't refresh your settings. Showing the last copy."
            : "Couldn't load your settings.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  useScrollMemory(form !== null);

  const setWindow = (
    meal: (typeof WINDOW_MEALS)[number],
    field: "start" | "end",
    value: string,
  ) =>
    edit((f) => ({
      ...f,
      meal_windows: {
        ...f.meal_windows,
        [meal]: { ...f.meal_windows[meal], [field]: value },
      } as MealWindows,
    }));

  // Only what this page's Save writes counts: the Oura card, API keys and
  // the wizard save on their own.
  const dirty =
    form != null &&
    saved != null &&
    (JSON.stringify(targets) !== JSON.stringify(targetText(saved.targets)) ||
      JSON.stringify(form.meal_windows) !== JSON.stringify(saved.meal_windows) ||
      form.timezone !== saved.timezone ||
      form.profile_notes !== saved.profile_notes);

  const discard = () => {
    if (saved) load(saved);
    setError(null);
    setTargetsInvalid(false);
  };

  const save = async () => {
    if (!form) return;
    const parsed: Partial<Targets> = {};
    for (const f of TARGET_FIELDS) {
      const n = Number.parseFloat(targets[f.key]);
      if (Number.isFinite(n) && n > 0) parsed[f.key] = n;
    }
    // All four: a half-filled target set would make the tracker's
    // remaining-vs-target rings lie for the missing macros, and a set can't
    // be cleared (PUT has no way to say so). None only while there are none.
    const filled = TARGET_FIELDS.filter((f) => parsed[f.key] != null).length;
    if (filled !== TARGET_FIELDS.length && (filled !== 0 || !targetsEmpty(form))) {
      setError("Fill in all four targets.");
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
      writes.current += 1;
      flashStatus("Saved.");
    } catch {
      setError("Couldn't save — try again.");
    } finally {
      setBusy(false);
    }
  };

  // Closing the wizard gives focus back to what opened it. A "Set up" that
  // went once targets were saved (or no opener, from ?setup=targets) leaves
  // it on <body>: the button in its place takes it.
  const wizardBtn = useRef<HTMLButtonElement>(null);
  const wizardWas = useRef(wizard);
  useEffect(() => {
    if (wizardWas.current && !wizard && document.activeElement === document.body) {
      wizardBtn.current?.focus({ preventScroll: true });
    }
    wizardWas.current = wizard;
  }, [wizard]);

  const deviceTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const deviceZone = deviceTz ? zoneGeneric(deviceTz) : null;
  const body = form ? describeBody(form.body) : null;
  const noTargets = form != null && targetsEmpty(form);

  // The wizard is an overlay over the page, not in its place: the page keeps
  // its scroll and its cards (no refetch) for the return. inert keeps Tab
  // and the screen reader out of what it covers.
  return (
    <>
    <main className="shell settings" inert={wizard && form != null}>
      <header className="brand">
        <h1>Settings</h1>
      </header>

      {/* a failed read, or a refresh that failed over the cached form
          (readCache); a refused save shows in the save bar instead */}
      {error && (!form || !dirty) ? (
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
                    ref={wizardBtn}
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
                    <span className="m3-unit-wrap">
                      <input
                        inputMode="decimal"
                        value={targets[f.key]}
                        onChange={(e) => {
                          setTargets((t) => ({ ...t, [f.key]: e.target.value }));
                          setTargetsInvalid(false);
                          edited();
                        }}
                        aria-invalid={
                          targetsInvalid && !(Number.parseFloat(targets[f.key]) > 0)
                        }
                        placeholder="—"
                        aria-label={`${f.label} target (${f.unit})`}
                        className="num"
                      />
                      <span className="m3-unit" aria-hidden="true">
                        {f.unit}
                      </span>
                    </span>
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
                      ref={wizardBtn}
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
                  <span>{mealLabel(m)}</span>
                  <input
                    type="time"
                    value={form.meal_windows[m].start}
                    onChange={(e) => setWindow(m, "start", e.target.value)}
                    aria-label={`${mealLabel(m)} start`}
                  />
                  <input
                    type="time"
                    value={form.meal_windows[m].end}
                    onChange={(e) => setWindow(m, "end", e.target.value)}
                    aria-label={`${mealLabel(m)} end`}
                  />
                </div>
              ))}
              <div className="m3-window">
                <span>Any other time</span>
                <select
                  value={form.meal_windows.default}
                  onChange={(e) =>
                    edit((f) => ({
                      ...f,
                      meal_windows: { ...f.meal_windows, default: e.target.value },
                    }))
                  }
                  aria-label="Meal at any other time"
                >
                  {MEALS.map((meal) => (
                    <option key={meal} value={meal}>
                      {mealLabel(meal)}
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

          {/* A picker, not a typed IANA id: a typo was a 422. The stored zone
              is offered first when this browser's list lacks it ("UTC", an
              old alias), so the select can still hold it. No "automatic"
              mode — the server keeps one zone and nothing syncs it. */}
          <section className="m3-section" aria-label="Time zone">
            <h2>Time zone</h2>
            <div className="m3-card">
              <div className="m3-field">
                <select
                  value={form.timezone}
                  onChange={(e) => edit((f) => ({ ...f, timezone: e.target.value }))}
                  aria-label="Time zone"
                >
                  {zones.ids.has(form.timezone) ? null : (
                    <option value={form.timezone}>{zoneLabel(form.timezone)}</option>
                  )}
                  {zones.groups.map(([region, list]) => (
                    <optgroup label={region} key={region}>
                      {list.map((z) => (
                        <option value={z.id} key={z.id}>
                          {z.label}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </div>
              {deviceTz && deviceTz !== form.timezone ? (
                <button
                  type="button"
                  className="m3-ghost-btn"
                  onClick={() => edit((f) => ({ ...f, timezone: deviceTz }))}
                >
                  Use this phone&rsquo;s time zone
                  {deviceZone ? ` (${deviceZone})` : ""}
                </button>
              ) : null}
            </div>
          </section>

          <section className="m3-section m3-wide" aria-label="Profile notes">
            <h2>Profile notes</h2>
            <div className="m3-card">
              <label className="m3-field">
                <textarea
                  ref={notesRef}
                  value={form.profile_notes}
                  onChange={(e) =>
                    edit((f) => ({ ...f, profile_notes: e.target.value }))
                  }
                  aria-label="Profile notes"
                  placeholder={
                    "lactose intolerant; 'fairlife' always means Core Power Elite 42g; I train ~6pm; weekday lunch is the office cafeteria, its menu is at example.com"
                  }
                />
              </label>
              <p className="m3-note">
                What Tally should always know. It reads this every time you log
                or photograph a meal, and when it looks a food up. Add habits,
                what your words mean to you, and where you usually eat.
              </p>
            </div>
          </section>

          {/* Fixed above the tab bar, so it is in reach from whichever
              section was edited; a refused save says why here, beside it. */}
          {dirty ? (
            <div className="m3-savebar">
              {error ? (
                <p className="m3-savebar-note error" role="alert">
                  {error}
                </p>
              ) : (
                <p className="m3-savebar-note">Unsaved changes</p>
              )}
              <button
                type="button"
                className="m3-savebar-discard"
                disabled={busy}
                onClick={discard}
              >
                Discard
              </button>
              <button
                type="button"
                className="m3-savebar-save"
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
                  "Save"
                )}
              </button>
            </div>
          ) : null}
          {/* kept mounted, so "Saved." is announced as it appears */}
          <div className="toast-host" role="status">
            {status ? (
              <div className="toast">
                <span>{status}</span>
              </div>
            ) : null}
          </div>
        </>
      )}

      {/* Neither needs the settings read, so a failed one doesn't hide them.
          While it is out they wait: under the short skeletons they would
          drop most of a screen when the form lands. */}
      {form || error ? (
        <>
          <ApiKeysCard />

          <section className="m3-section" aria-label="About">
            <h2>About</h2>
            {/* The docs and the legal pages are the public website: opened
                over the app (iOS's Safari sheet with Done, a new tab
                elsewhere) rather than in its window, whose only way back
                from them is the logo. */}
            <div className="m3-card m3-links">
              {/* owner only (Bill, 2026-10-13, BACKLOG 105): the charts are the
                  owner's §9 instrument; a demo can still open /experiment by
                  URL and sees its own series */}
              {me?.is_owner ? (
                <Link href="/experiment" className="m3-nav-row">
                  Experiment charts
                  <span className="chev" aria-hidden="true" />
                </Link>
              ) : null}
              <a
                href="/docs/api-keys"
                target="_blank"
                rel="noopener"
                className="m3-nav-row"
              >
                How to use API keys
                <span className="chev" aria-hidden="true" />
              </a>
              <a href="/privacy" target="_blank" rel="noopener" className="m3-nav-row">
                Privacy
                <span className="chev" aria-hidden="true" />
              </a>
              <a href="/terms" target="_blank" rel="noopener" className="m3-nav-row">
                Terms
                <span className="chev" aria-hidden="true" />
              </a>
            </div>
            <p className="m3-note">
              Deleting the home-screen icon clears the app&rsquo;s on-device
              state (your sign-in, any late items still sending). Your diary
              lives on the server and is untouched.
            </p>
            <p className="m3-note">Data: USDA FoodData Central · Open Food Facts</p>
          </section>
        </>
      ) : null}
    </main>
    {wizard && form ? (
      <WizardGoal
        initial={form.body}
        onDone={(s) => {
          // The wizard wrote targets and body only: take just those, so
          // unsaved meal-window, time zone and notes edits outlive it.
          setForm((f) => (f ? { ...f, targets: s.targets, body: s.body } : s));
          setSaved((v) => (v ? { ...v, targets: s.targets, body: s.body } : s));
          setTargets(targetText(s.targets));
          put("settings", s);
          writes.current += 1;
          putTodayTargets(s.targets);
          setWizard(false);
          flashStatus("Targets saved.");
        }}
        onCancel={() => setWizard(false)}
      />
    ) : null}
    </>
  );
}
