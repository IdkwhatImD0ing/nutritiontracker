"use client";

import { useLayoutEffect, useRef, useState } from "react";
import { ApiError, type DayTotals, type Meal } from "@/lib/api";
import { portionText } from "@/lib/format";
import { foodName, mealLabel } from "@/lib/labels";
import "@/app/history/diary.css";
import {
  deleteEntry,
  patchEntry,
  type DiaryEntry,
  type DiaryMeals,
} from "./DiaryApi";
import DiaryEntryRow from "./DiaryEntryRow";
import { createMeal, type SavedMeal } from "./LibraryApi";
import SheetHeader from "./SheetHeader";
import { useAutoGrow } from "./useAutoGrow";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const MEAL_ORDER: Meal[] = ["breakfast", "lunch", "dinner", "snack"];

/** Where a diary row's top sits on screen now (undefined: not rendered). */
function rowTop(root: HTMLElement | null, id: string): number | undefined {
  return root
    ?.querySelector(`[data-entry-id="${CSS.escape(id)}"]`)
    ?.getBoundingClientRect().top;
}

type Props = {
  meals: DiaryMeals;
  /** a PATCH landed — the parent swaps the entry and takes the new totals */
  onPatched: (entry: DiaryEntry, totals: DayTotals) => void;
  /** a DELETE landed */
  onDeleted: (entryId: string, totals: DayTotals) => void;
  emptyText?: string;
};

/** §8.4 create path 2 — "tap Save as meal on any meal group". The group's
 *  committed rows are the macro's fixed items (food, serving, qty exactly as
 *  committed); the server norms the trigger phrases and falls back to
 *  norm(name) when none are given. */
function SaveMealSheet({
  meal,
  entries,
  onSaved,
  onClose,
}: {
  meal: string;
  entries: DiaryEntry[];
  onSaved: (m: SavedMeal) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(`usual ${meal}`);
  const [phrases, setPhrases] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const phrasesRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(phrasesRef, phrases);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(
        await createMeal({
          name: name.trim(),
          trigger_phrases: phrases
            .split(/[,\n]/)
            .map((s) => s.trim())
            .filter(Boolean),
          items: entries.map((e) => ({
            food_id: e.food.id,
            serving_id: e.serving.id,
            qty: e.qty,
          })),
        }),
      );
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "You already have a saved meal with that name — rename it or edit it in Library."
          : "Couldn't save that meal — try again.",
      );
      setBusy(false);
    }
  };

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Save as meal">
        <SheetHeader title={`Save this ${meal} as a meal`} onClose={onClose} />

        <label className="m3-field">
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            autoComplete="off"
          />
        </label>

        <label className="m3-field">
          Phrases
          {/* a long list wraps and grows instead of scrolling sideways in
              one line (useAutoGrow) */}
          <textarea
            ref={phrasesRef}
            className="m3-phrases"
            rows={2}
            value={phrases}
            onChange={(e) => setPhrases(e.target.value)}
            placeholder={`e.g. usual ${meal}, my ${meal}`}
            autoComplete="off"
          />
        </label>
        <p className="m3-note">
          Separate phrases with commas or new lines; leave empty to use the
          name. Saying one adds these{" "}
          {entries.length} item{entries.length === 1 ? "" : "s"} exactly as they
          are now.
        </p>

        {/* Library's item layout: the portion on its own line under the
            name, so a long serving never squeezes into '1 mug / (12 fl / oz)' */}
        <div className="m3-card">
          {entries.map((e) => (
            <div className="m3-item" key={e.id}>
              <div className="info">
                <div className="name">{foodName(e.food.name)}</div>
                <div className="sub num">
                  {portionText(e.qty, e.serving.description, e.grams)}
                </div>
              </div>
            </div>
          ))}
        </div>

        {error ? <p className="sheet-error">{error}</p> : null}

        <button
          type="button"
          className="commit-btn"
          disabled={busy || !name.trim()}
          onClick={save}
        >
          {busy ? "Saving…" : "Save meal"}
        </button>
      </div>
    </>
  );
}

/** The diary grouped by meal (§7.4 Today / History day view): meal header
 *  with its kcal subtotal, then §7.1-style rows. Edits go straight to
 *  `/api/entries/:id`; the parent owns the diary state. */
export default function DiaryList({
  meals,
  onPatched,
  onDeleted,
  emptyText = "Nothing logged yet.",
}: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saveGroup, setSaveGroup] = useState<{
    meal: string;
    entries: DiaryEntry[];
  } | null>(null);
  const [savedMeal, setSavedMeal] = useState<string | null>(null);

  // The tapped row stays under the finger. One row is open at a time, so
  // opening one closes the last; when that one sat above, its ~270px editor
  // collapsing threw the tapped row up the screen. iOS Safari has no CSS
  // scroll anchoring, so the row's top is read before the toggle and the
  // window (the scroller on Today and the day view) is moved by however far
  // it went.
  const sectionRef = useRef<HTMLElement>(null);
  const anchor = useRef<{ id: string; top: number } | null>(null);
  useLayoutEffect(() => {
    const a = anchor.current;
    anchor.current = null;
    if (!a) return;
    const top = rowTop(sectionRef.current, a.id);
    if (top != null && top !== a.top) window.scrollBy(0, top - a.top);
  }, [expanded]);

  const groups = MEAL_ORDER.map((meal) => ({
    meal,
    entries: meals[meal] ?? [],
  })).filter((g) => g.entries.length > 0);
  // a meal value outside the four (defensive: the server groups by string)
  for (const [meal, entries] of Object.entries(meals)) {
    if (!MEAL_ORDER.includes(meal as Meal) && entries.length > 0) {
      groups.push({ meal: meal as Meal, entries });
    }
  }

  if (groups.length === 0) {
    return <p className="diary-empty">{emptyText}</p>;
  }

  /** null when `fn` landed, else the sentence for the refusal — the portion
   *  sheet shows it in place; a delete's goes over the diary. */
  const run = async (
    id: string,
    fn: () => Promise<void>,
  ): Promise<string | null> => {
    setBusy(id);
    setError(null);
    try {
      await fn();
      return null;
    } catch (e) {
      // A refused portion says why (a serving with partial macros is a 422
      // naming it, §4 invariant) — show the server's sentence, not a generic
      // one, the way every other lane does.
      return (
        (e instanceof ApiError && e.status === 422 && e.detail) ||
        "Couldn't save that change — try again."
      );
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="diary" aria-label="Diary" ref={sectionRef}>
      {error ? <p className="capture-error">{error}</p> : null}
      {groups.map((g) => (
        <div className="diary-meal" key={g.meal}>
          <div className="diary-meal-head">
            <span className="diary-meal-name">{mealLabel(g.meal)}</span>
            <button
              type="button"
              className="m3-link-btn"
              onClick={() => setSaveGroup(g)}
            >
              Save as meal
            </button>
            <span className="diary-meal-kcal num">
              {fmt.format(g.entries.reduce((s, e) => s + e.macros.kcal, 0))} kcal
            </span>
          </div>
          {g.entries.map((e) => (
            <DiaryEntryRow
              key={e.id}
              entry={e}
              expanded={expanded === e.id}
              busy={busy === e.id}
              onToggle={() => {
                const top = rowTop(sectionRef.current, e.id);
                anchor.current = top != null ? { id: e.id, top } : null;
                setExpanded((cur) => (cur === e.id ? null : e.id));
              }}
              onSave={(patch) =>
                run(e.id, async () => {
                  const res = await patchEntry(e.id, patch);
                  // routes/entries.py builds its DiaryEntry without the
                  // source/citation/estimate fields — keep the ones already
                  // rendered (`grams` it does send, and a serving change
                  // moves it, so that one comes from the response)
                  onPatched(
                    {
                      ...res.entry,
                      web_source: e.web_source,
                      source: e.source,
                      estimated: e.estimated,
                    },
                    res.totals,
                  );
                })
              }
              onDelete={() =>
                void run(e.id, async () => {
                  const totals = await deleteEntry(e.id);
                  onDeleted(e.id, totals);
                }).then(setError)
              }
            />
          ))}
        </div>
      ))}
      {savedMeal ? <p className="m3-status">{savedMeal}</p> : null}
      {saveGroup ? (
        <SaveMealSheet
          meal={saveGroup.meal}
          entries={saveGroup.entries}
          onSaved={(m) => {
            setSaveGroup(null);
            setSavedMeal(`Saved as “${m.name}” — say it to log it again.`);
          }}
          onClose={() => setSaveGroup(null)}
        />
      ) : null}
    </section>
  );
}
