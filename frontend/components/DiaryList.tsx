"use client";

import { useState } from "react";
import { ApiError, type DayTotals, type Meal } from "@/lib/api";
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
import { useFlash } from "./useFlash";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmtQ = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const MEAL_ORDER: Meal[] = ["breakfast", "lunch", "dinner", "snack"];

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

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      onSaved(
        await createMeal({
          name: name.trim(),
          trigger_phrases: phrases
            .split(",")
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
          Trigger phrases
          <input
            value={phrases}
            onChange={(e) => setPhrases(e.target.value)}
            placeholder="usual breakfast, my breakfast"
            autoComplete="off"
          />
        </label>
        <p className="m3-note">
          Comma-separated; leave empty to use the name. Saying one stages these{" "}
          {entries.length} item{entries.length === 1 ? "" : "s"} exactly as they
          are now — no search, no AI.
        </p>

        <div className="m3-card">
          {entries.map((e) => (
            <div className="m3-row" key={e.id}>
              <span className="k">{e.food.name}</span>
              <span className="num">
                {e.qty !== 1 ? `${fmtQ.format(e.qty)} × ` : ""}
                {e.serving.description}
              </span>
            </div>
          ))}
        </div>

        {error ? <p className="sheet-error">{error}</p> : null}

        <button
          type="button"
          className="commit-btn"
          disabled={busy || !name.trim()}
          aria-busy={busy}
          onClick={save}
        >
          {busy ? (
            <>
              <span className="spinner on-accent" />
              Saving…
            </>
          ) : (
            "Save meal"
          )}
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
  // which row is waiting, and on what — the spinner goes in the tapped button
  const [busy, setBusy] = useState<{
    id: string;
    kind: "save" | "delete";
  } | null>(null);
  // a failed edit, kept with its row so it shows where the user is looking
  const [error, setError] = useState<{ id: string; text: string } | null>(null);
  const [saveGroup, setSaveGroup] = useState<{
    meal: string;
    entries: DiaryEntry[];
  } | null>(null);
  // A toast, not a line under the last meal: saving Breakfast put that line
  // off screen. Rendered here, so Today and the History day both get it;
  // under-sheets, like Today's commit note, so a sheet opened next covers it.
  const [savedMeal, flashSavedMeal] = useFlash(4000);

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

  /** true when `fn` landed — the row keeps its draft on a refusal. */
  const run = async (
    id: string,
    kind: "save" | "delete",
    fn: () => Promise<void>,
  ): Promise<boolean> => {
    setBusy({ id, kind });
    setError(null);
    try {
      await fn();
      return true;
    } catch (e) {
      // A refused portion says why (a serving with partial macros is a 422
      // naming it, §4 invariant) — show the server's sentence, not a generic
      // one, the way every other lane does.
      setError({
        id,
        text:
          (e instanceof ApiError && e.status === 422 && e.detail) ||
          "Couldn't save that change — try again.",
      });
      return false;
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="diary" aria-label="Diary">
      {groups.map((g) => (
        <div className="diary-meal" key={g.meal}>
          <div className="diary-meal-head">
            <span className="diary-meal-name">{g.meal}</span>
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
              busy={busy?.id === e.id ? busy.kind : null}
              error={error?.id === e.id ? error.text : null}
              onToggle={() => {
                // an error belongs to the open row; any toggle closes that row
                setError(null);
                setExpanded((cur) => (cur === e.id ? null : e.id));
              }}
              onSave={(patch) =>
                run(e.id, "save", async () => {
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
                run(e.id, "delete", async () => {
                  const totals = await deleteEntry(e.id);
                  onDeleted(e.id, totals);
                })
              }
            />
          ))}
        </div>
      ))}
      {savedMeal ? (
        <div className="toast under-sheets" role="status">
          <span>{savedMeal}</span>
          <button type="button" onClick={() => flashSavedMeal(null)}>
            OK
          </button>
        </div>
      ) : null}
      {saveGroup ? (
        <SaveMealSheet
          meal={saveGroup.meal}
          entries={saveGroup.entries}
          onSaved={(m) => {
            setSaveGroup(null);
            flashSavedMeal(`Saved as “${m.name}” — say it to log it again.`);
          }}
          onClose={() => setSaveGroup(null)}
        />
      ) : null}
    </section>
  );
}
