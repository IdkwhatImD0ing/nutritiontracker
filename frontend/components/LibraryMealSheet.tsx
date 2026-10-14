"use client";

import { useState, type FormEvent } from "react";
import {
  ApiError,
  searchFoods,
  type FoodSearchResult,
  type StagedServing,
} from "@/lib/api";
import {
  cacheFood,
  createMeal,
  getFood,
  patchMeal,
  type MealPatch,
  type SavedMeal,
  type SavedMealItem,
} from "./LibraryApi";
import SheetHeader from "./SheetHeader";

type Props = {
  /** null → §8.4 create path 3, "build one in Library" (POST /api/meals) */
  meal: SavedMeal | null;
  onSaved: (m: SavedMeal) => void;
  onClose: () => void;
};

const fmtQ = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

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

  // create mode only: the food search that fills the item list. Servings are
  // kept per food so the picked portion can be changed without another fetch.
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState(false);
  const [servingsByFood, setServingsByFood] = useState<
    Record<string, StagedServing[]>
  >({});

  const setQty = (i: number, qty: number) =>
    setItems((prev) =>
      prev.map((it, j) =>
        j === i ? { ...it, qty: Math.max(0.25, Math.round(qty * 100) / 100) } : it,
      ),
    );
  const removeItem = (i: number) =>
    setItems((prev) => prev.filter((_, j) => j !== i));

  const setServing = (i: number, serving_id: string) =>
    setItems((prev) =>
      prev.map((it, j) => {
        if (j !== i) return it;
        const s = (servingsByFood[it.food_id] ?? []).find(
          (x) => x.serving_id === serving_id,
        );
        return { ...it, serving_id, serving_description: s?.description ?? serving_id };
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
    setAdding(true);
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
      setAdding(false);
    }
  };

  const save = async () => {
    const patch: MealPatch = {
      name: name.trim(),
      trigger_phrases: phrases
        .split(",")
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

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Edit saved meal">
        <SheetHeader
          title={!meal ? "New saved meal" : meal.is_active ? "Saved meal" : "Inactive meal"}
          onClose={onClose}
        />

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
          Comma-separated. Saying any of these stages the items below exactly as
          stored — no search, no AI.
        </p>

        <div>
          <div className="edit-label">Items</div>
          {items.map((it, i) => (
            <div className="m3-item" key={`${it.food_id}:${it.serving_id}:${i}`}>
              <div className="info">
                <div className="name">{it.food_name ?? it.food_id}</div>
                {servingsByFood[it.food_id] ? (
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
                  onClick={() => setQty(i, it.qty - 0.5)}
                >
                  −
                </button>
                <span className="q">{fmtQ.format(it.qty)}</span>
                <button
                  type="button"
                  aria-label="More"
                  onClick={() => setQty(i, it.qty + 0.5)}
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
                ✕
              </button>
            </div>
          ))}
          {items.length === 0 ? (
            <p className="subsheet-hint">
              {meal
                ? "No items — a meal needs at least one. Re-save a log as this meal to rebuild it."
                : "No items yet — search below and tap a result to add one."}
            </p>
          ) : null}
        </div>

        {!meal ? (
          <div>
            <div className="edit-label">Add a food</div>
            <form className="add-item" onSubmit={runSearch}>
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search the food database"
                aria-label="Search foods"
                autoComplete="off"
              />
              <button
                type="submit"
                disabled={searching || adding || !query.trim()}
                aria-label="Search"
              >
                {searching ? <span className="spinner" /> : "→"}
              </button>
            </form>
            {results ? (
              <div className="search-results">
                {results.map((r, i) => (
                  <button
                    key={`${r.source}:${r.source_id}:${i}`}
                    type="button"
                    disabled={adding}
                    onClick={() => addHit(r)}
                  >
                    <span className="result-name">{r.label}</span>
                    <span className="result-sub">
                      {r.brand ? `${r.brand} · ` : ""}
                      {r.food_id ? "my foods" : (r.data_type ?? r.source)}
                    </span>
                  </button>
                ))}
                {results.length === 0 && !searching ? (
                  <p className="subsheet-hint">No matches — try fewer words.</p>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}

        {error ? <p className="sheet-error">{error}</p> : null}

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
            "Create meal"
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
      </div>
    </>
  );
}
