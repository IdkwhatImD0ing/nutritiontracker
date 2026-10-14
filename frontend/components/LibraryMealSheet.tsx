"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ApiError,
  searchFoods,
  type FoodSearchResult,
  type StagedServing,
} from "@/lib/api";
import { foodName } from "@/lib/labels";
import { hitSub } from "@/lib/sources";
import {
  cacheFood,
  createMeal,
  getFood,
  patchMeal,
  type MealPatch,
  type SavedMeal,
  type SavedMealItem,
} from "./LibraryApi";
import { FOOD_FIELD_PROPS } from "./fieldProps";
import { ArrowRight, Close } from "./icons";
import { qtyInput, rescaleQty, stepQty } from "./portion";
import SheetHeader, { DiscardConfirm } from "./SheetHeader";
import { useAutoGrow } from "./useAutoGrow";

type Props = {
  /** null → §8.4 create path 3, "build one in Library" (POST /api/meals) */
  meal: SavedMeal | null;
  onSaved: (m: SavedMeal) => void;
  onClose: () => void;
};

/** The qty an item takes: a quarter at least, to 2 places. */
const normQty = (qty: number) => Math.max(0.25, Math.round(qty * 100) / 100);

/** An item's qty as a field you can type into, between the ±0.5 steppers:
 *  the typed text while the item holds the qty it typed, the item's qty
 *  otherwise — so a step from the ± buttons mid-edit shows, not the text. */
function QtyInput({
  qty,
  label,
  onQty,
}: {
  qty: number;
  label: string;
  onQty: (qty: number) => void;
}) {
  const [draft, setDraft] = useState<{ text: string; qty: number } | null>(
    null,
  );
  return (
    <input
      className="q"
      inputMode="decimal"
      aria-label={label}
      // qtyInput, not a grouped format: Number("1,200") is NaN, and a gram
      // serving's qty (rescaled or stepped by 10) easily reaches 1,000
      value={draft && draft.qty === qty ? draft.text : qtyInput(qty)}
      onChange={(e) => {
        const n = Number(e.target.value);
        const ok = Number.isFinite(n) && n > 0;
        setDraft({ text: e.target.value, qty: ok ? normQty(n) : qty });
        if (ok) onQty(n);
      }}
      onBlur={() => setDraft(null)}
    />
  );
}

/** §8.4 saved meals: build a new one (search → add foods) or edit an existing
 *  one — name, trigger phrases, the fixed item list (qty / remove),
 *  deactivate. Delete = deactivate, so old diary rows keep their context;
 *  normalization of trigger phrases happens server-side (§5). */
export default function LibraryMealSheet({ meal, onSaved, onClose }: Props) {
  const [name, setName] = useState(meal?.name ?? "");
  const [phrases, setPhrases] = useState(meal?.trigger_phrases.join(", ") ?? "");
  const [items, setItems] = useState<SavedMealItem[]>(meal?.items ?? []);
  // which request is in flight: only Save / Create shows a spinner for its own
  const [busy, setBusy] = useState<"save" | "toggle" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const phrasesRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(phrasesRef, phrases);
  // a close that would lose edits asks first, in place of Save
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  // The food search that adds to the item list — in an edit too (§8.4: "change
  // the item list in Library"). Servings are kept per food so the picked
  // portion can be changed without another fetch.
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  // the hit being added (`source:source_id`), marked with a spinner
  const [adding, setAdding] = useState<string | null>(null);
  const [servingsByFood, setServingsByFood] = useState<
    Record<string, StagedServing[]>
  >({});

  // Editing: each stored item's servings, so it gets the picker an added one
  // has. A food that fails to load keeps its serving as plain text.
  useEffect(() => {
    if (!meal) return;
    let cancelled = false;
    const ids = [...new Set(meal.items.map((it) => it.food_id))];
    Promise.allSettled(ids.map((id) => getFood(id))).then((res) => {
      if (cancelled) return;
      const found: Record<string, StagedServing[]> = {};
      res.forEach((r, i) => {
        if (r.status === "fulfilled" && r.value.servings?.length) {
          found[ids[i]] = r.value.servings;
        }
      });
      setServingsByFood((m) => ({ ...found, ...m }));
    });
    return () => {
      cancelled = true;
    };
  }, [meal]);

  const setQty = (i: number, qty: number) =>
    setItems((prev) =>
      prev.map((it, j) =>
        j === i ? { ...it, qty: normQty(qty) } : it,
      ),
    );
  const removeItem = (i: number) =>
    setItems((prev) => prev.filter((_, j) => j !== i));

  /** one serving's weight for an item, when its servings were fetched */
  const gramsOf = (food_id: string, serving_id: string) =>
    (servingsByFood[food_id] ?? []).find((x) => x.serving_id === serving_id)
      ?.grams;

  // Another serving counts the same food: the qty is converted with it when
  // both weights are known ("0.5 × 1 cup" becomes 74 g, not 0.5 g).
  const setServing = (i: number, serving_id: string) =>
    setItems((prev) =>
      prev.map((it, j) => {
        if (j !== i) return it;
        const s = (servingsByFood[it.food_id] ?? []).find(
          (x) => x.serving_id === serving_id,
        );
        const qty =
          rescaleQty(it.qty, gramsOf(it.food_id, it.serving_id), s?.grams) ??
          it.qty;
        return {
          ...it,
          serving_id,
          qty,
          serving_description: s?.description ?? serving_id,
        };
      }),
    );

  const run = async (patch: MealPatch, kind: "save" | "toggle") => {
    if (!meal) return;
    setBusy(kind);
    setError(null);
    try {
      onSaved(await patchMeal(meal.id, patch));
    } catch {
      setError("Couldn't save — try again.");
      setBusy(null);
    }
  };

  const runSearch = async (e: FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setError(null);
    try {
      setResults((await searchFoods(q)).hits);
    } catch {
      setResults([]);
      setError("Search failed. Try again.");
    } finally {
      setSearching(false);
    }
  };

  /** A hit becomes an item at its default serving, qty 1 — the serving picker
   *  and stepper on the row take it from there (§8.5). FDC hits are cached
   *  first so the macro can reference a foods row. */
  const addHit = async (hit: FoodSearchResult) => {
    setAdding(`${hit.source}:${hit.source_id}`);
    setError(null);
    try {
      const food = await getFood(await cacheFood(hit));
      const servings = food.servings ?? [];
      const s =
        servings.find((x) => x.serving_id === food.default_serving_id) ??
        servings[0];
      if (!s) throw new ApiError(422, "that food has no serving definitions");
      setServingsByFood((m) => ({ ...m, [food.id]: servings }));
      setItems((prev) => [
        ...prev,
        {
          food_id: food.id,
          serving_id: s.serving_id,
          qty: 1,
          food_name: food.display_name,
          brand: food.brand ?? null,
          serving_description: s.description,
        },
      ]);
      setQuery("");
      setResults(null);
    } catch {
      setError("Couldn't add that food.");
    } finally {
      setAdding(null);
    }
  };

  const save = async () => {
    const patch: MealPatch = {
      name: name.trim(),
      trigger_phrases: phrases
        .split(/[,\n]/)
        .map((s) => s.trim())
        .filter(Boolean),
      items: items.map(({ food_id, serving_id, qty }) => ({
        food_id,
        serving_id,
        qty,
      })),
    };
    if (meal) {
      run(patch, "save");
      return;
    }
    setBusy("save");
    setError(null);
    try {
      onSaved(
        await createMeal({
          name: patch.name ?? "",
          trigger_phrases: patch.trigger_phrases ?? [],
          items: patch.items ?? [],
        }),
      );
    } catch (e) {
      setError(
        e instanceof ApiError && e.status === 409
          ? "You already have a saved meal with that name."
          : "Couldn't save — try again.",
      );
      setBusy(null);
    }
  };

  const valid = name.trim().length > 0 && items.length > 0;

  // Edits a backdrop tap, the ✕, a pull, Escape or Back would throw away:
  // anything changed from what the sheet opened with (a new meal: anything
  // typed or added). Those closes ask first, in the foot.
  const itemKey = (list: SavedMealItem[]) =>
    JSON.stringify(list.map(({ food_id, serving_id, qty }) => [food_id, serving_id, qty]));
  const dirty = meal
    ? name !== meal.name ||
      phrases !== meal.trigger_phrases.join(", ") ||
      itemKey(items) !== itemKey(meal.items)
    : name.trim() !== "" || phrases.trim() !== "" || items.length > 0;
  const requestClose = () => {
    if (dirty) setConfirmDiscard(true);
    else onClose();
  };

  return (
    <>
      <div className="subsheet-backdrop" onClick={requestClose} />
      <div className="subsheet" role="dialog" aria-label="Edit saved meal">
        <SheetHeader
          title={!meal ? "New saved meal" : meal.is_active ? "Saved meal" : "Inactive meal"}
          onClose={requestClose}
        />

        <label className="m3-field">
          Name
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={meal ? undefined : "e.g. Protein snack"}
            autoComplete="off"
          />
        </label>

        <label className="m3-field">
          Phrases
          {/* a long list wraps and grows instead of scrolling sideways in
              one line (useAutoGrow). The example is for a new meal only: in
              an edit it read as this meal's own phrases. */}
          <textarea
            ref={phrasesRef}
            className="m3-phrases"
            rows={2}
            value={phrases}
            onChange={(e) => setPhrases(e.target.value)}
            placeholder={meal ? undefined : "e.g. usual breakfast, my breakfast"}
            autoComplete="off"
          />
        </label>
        <p className="m3-note">
          Separate phrases with commas or new lines; leave empty to use the
          name. Saying any of these adds the items below exactly as saved,
          without looking anything up.
        </p>

        <div>
          <div className="edit-label">Items</div>
          {items.map((it, i) => (
            <div className="m3-item" key={`${it.food_id}:${it.serving_id}:${i}`}>
              <div className="info">
                <div className="name">
                  {it.food_name ? foodName(it.food_name) : it.food_id}
                </div>
                {/* a stored serving the food no longer lists stays text */}
                {servingsByFood[it.food_id]?.some(
                  (s) => s.serving_id === it.serving_id,
                ) ? (
                  <div className="m3-field">
                    <select
                      value={it.serving_id}
                      onChange={(e) => setServing(i, e.target.value)}
                      aria-label={`Serving for ${it.food_name ?? "item"}`}
                    >
                      {servingsByFood[it.food_id].map((s) => (
                        <option key={s.serving_id} value={s.serving_id}>
                          {s.description}
                        </option>
                      ))}
                    </select>
                  </div>
                ) : (
                  <div className="sub">
                    {it.brand ? `${it.brand} · ` : ""}
                    {it.serving_description ?? it.serving_id}
                  </div>
                )}
              </div>
              <div className="m3-item-qty num">
                <button
                  type="button"
                  aria-label="Less"
                  onClick={() =>
                    setQty(
                      i,
                      stepQty(it.qty, -1, gramsOf(it.food_id, it.serving_id), 0.5),
                    )
                  }
                >
                  −
                </button>
                <QtyInput
                  qty={it.qty}
                  label={`Quantity of ${it.food_name ?? "item"}`}
                  onQty={(q) => setQty(i, q)}
                />
                <button
                  type="button"
                  aria-label="More"
                  onClick={() =>
                    setQty(
                      i,
                      stepQty(it.qty, 1, gramsOf(it.food_id, it.serving_id), 0.5),
                    )
                  }
                >
                  +
                </button>
              </div>
              <button
                type="button"
                className="row-remove"
                aria-label={`Remove ${it.food_name ?? "item"}`}
                onClick={() => removeItem(i)}
              >
                <Close />
              </button>
            </div>
          ))}
          {items.length === 0 ? (
            <p className="subsheet-hint">
              {meal
                ? "No items — add a food below."
                : "No items yet — search below and tap a result to add one."}
            </p>
          ) : null}
        </div>

        <div>
          <div className="edit-label">Add a food</div>
          <form className="add-item" onSubmit={runSearch}>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search foods"
              aria-label="Search foods"
              autoComplete="off"
              {...FOOD_FIELD_PROPS}
              enterKeyHint="search"
            />
            <button
              type="submit"
              disabled={searching || adding != null || !query.trim()}
              aria-label="Search"
            >
              {searching ? (
                <span className="spinner" />
              ) : (
                <ArrowRight strokeWidth={2.4} />
              )}
            </button>
          </form>
          {results ? (
            <div className="search-results">
              {results.map((r, i) => (
                <button
                  key={`${r.source}:${r.source_id}:${i}`}
                  type="button"
                  disabled={adding != null}
                  aria-busy={adding === `${r.source}:${r.source_id}` || undefined}
                  onClick={() => addHit(r)}
                >
                  <span className="result-name">{foodName(r.label)}</span>
                  <span className="result-sub">{hitSub(r)}</span>
                  {adding === `${r.source}:${r.source_id}` ? (
                    <span className="spinner" />
                  ) : null}
                </button>
              ))}
              {results.length === 0 && !searching ? (
                <p className="subsheet-hint">No matches — try fewer words.</p>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="subsheet-foot">
          {error ? <p className="sheet-error">{error}</p> : null}

          {confirmDiscard ? (
            <DiscardConfirm
              onKeep={() => setConfirmDiscard(false)}
              onDiscard={onClose}
            />
          ) : (
            <>
              <button
                type="button"
                className="commit-btn"
                disabled={busy !== null || !valid}
                aria-busy={busy === "save"}
                onClick={save}
              >
                {busy === "save" ? (
                  <>
                    <span className="spinner on-accent" />
                    Saving…
                  </>
                ) : meal ? (
                  "Save"
                ) : (
                  "Save meal"
                )}
              </button>

              {meal ? (
                <div className="m3-actions">
                  {meal.is_active ? (
                    <button
                      type="button"
                      className="m3-link-btn danger"
                      disabled={busy !== null}
                      onClick={() => run({ is_active: false }, "toggle")}
                    >
                      Deactivate
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="m3-link-btn"
                      disabled={busy !== null}
                      onClick={() => run({ is_active: true }, "toggle")}
                    >
                      Reactivate
                    </button>
                  )}
                  <span className="m3-note">Past diary entries keep their numbers.</span>
                </div>
              ) : null}
            </>
          )}
        </div>
      </div>
    </>
  );
}
