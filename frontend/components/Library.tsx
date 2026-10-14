"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { searchFoods, type FoodSearchResult } from "@/lib/api";
import {
  cacheFood,
  getAliases,
  getMeals,
  type AliasRow,
  type SavedMeal,
} from "./LibraryApi";
import LibraryAliasSheet from "./LibraryAliasSheet";
import LibraryMealSheet from "./LibraryMealSheet";

type FoodGroup = {
  food_id: string;
  food_name: string;
  brand: string | null;
  total: number;
  aliases: AliasRow[];
};

/** §7.4 Library: his foods = aliases grouped by food, most-used first. */
function groupByFood(rows: AliasRow[]): FoodGroup[] {
  const byFood = new Map<string, FoodGroup>();
  for (const a of rows) {
    let g = byFood.get(a.food_id);
    if (!g) {
      g = {
        food_id: a.food_id,
        food_name: a.food_name,
        brand: a.brand ?? null,
        total: 0,
        aliases: [],
      };
      byFood.set(a.food_id, g);
    }
    g.total += a.use_count;
    g.aliases.push(a);
  }
  const groups = [...byFood.values()];
  for (const g of groups) g.aliases.sort((x, y) => y.use_count - x.use_count);
  return groups.sort((x, y) => y.total - x.total);
}

function MealCard({
  meal,
  onOpen,
}: {
  meal: SavedMeal;
  onOpen: () => void;
}) {
  // Two phrases and a count: ten of them made a 4-line run-on of quotes. The
  // full list is the meal sheet's, and a CSS clamp would cut mid-quote.
  const more = meal.trigger_phrases.length - 2;
  return (
    <button
      type="button"
      className={`m3-card-btn${meal.is_active ? "" : " inactive"}`}
      onClick={onOpen}
    >
      <div className="m3-card-head">
        <span className="m3-card-title">{meal.name}</span>
        <span className="m3-card-stat num">
          {meal.items.length} item{meal.items.length === 1 ? "" : "s"}
          {meal.is_active ? "" : " · inactive"}
        </span>
      </div>
      <span className="m3-card-sub">
        {meal.trigger_phrases.length
          ? meal.trigger_phrases
              .slice(0, 2)
              .map((p) => `“${p}”`)
              .join(" · ") + (more > 0 ? ` +${more} more` : "")
          : "no trigger phrases"}
      </span>
    </button>
  );
}

export default function Library() {
  const router = useRouter();
  const [aliases, setAliases] = useState<AliasRow[] | null>(null);
  const [meals, setMeals] = useState<SavedMeal[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [fdcDown, setFdcDown] = useState(false);
  const [searching, setSearching] = useState(false);
  const [opening, setOpening] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  const [aliasSheet, setAliasSheet] = useState<AliasRow | null>(null);
  const [mealSheet, setMealSheet] = useState<SavedMeal | null>(null);
  /** the same sheet with no meal = the §8.4 "build one in Library" path */
  const [newMeal, setNewMeal] = useState(false);

  // `attempt` re-runs both reads: the failure notice carries a Retry rather
  // than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLoadError(null);
    Promise.allSettled([getAliases(), getMeals()]).then(([a, m]) => {
      if (cancelled) return;
      setAliases(a.status === "fulfilled" ? a.value : []);
      setMeals(m.status === "fulfilled" ? m.value : []);
      if (a.status === "rejected" || m.status === "rejected") {
        setLoadError("Couldn't load part of your library.");
      }
    });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const groups = useMemo(() => groupByFood(aliases ?? []), [aliases]);
  const activeMeals = (meals ?? []).filter((m) => m.is_active);
  const inactiveMeals = (meals ?? []).filter((m) => !m.is_active);

  const runSearch = async (e: FormEvent) => {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setSearching(true);
    setSearchError(null);
    try {
      const r = await searchFoods(q);
      setResults(r.hits);
      setFdcDown(r.fdcError != null);
    } catch {
      setResults([]);
      setSearchError("Search failed. Try again.");
    } finally {
      setSearching(false);
    }
  };

  // Manual search entry → food detail. FDC hits are cached first (the
  // existing /api/foods/select library path) so the detail page has a row.
  const openHit = async (hit: FoodSearchResult) => {
    setOpening(true);
    setSearchError(null);
    try {
      const id = await cacheFood(hit);
      router.push(`/library/food/${encodeURIComponent(id)}`);
    } catch {
      setSearchError("Couldn't open that food.");
      setOpening(false);
    }
  };

  return (
    <main className="shell library">
      <header className="brand">
        <h1>Library</h1>
        <span className="date">
          {aliases ? `${groups.length} food${groups.length === 1 ? "" : "s"}` : ""}
        </span>
      </header>

      {loadError ? (
        <div className="notice error" role="alert">
          <span>{loadError}</span>
          <button
            type="button"
            className="notice-btn"
            onClick={() => {
              setAliases(null);
              setMeals(null);
              setAttempt((n) => n + 1);
            }}
          >
            Retry
          </button>
        </div>
      ) : null}

      <section className="m3-section lib-search" aria-label="Find a food">
        <form className="capture" onSubmit={runSearch}>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search the food database"
            aria-label="Search foods"
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={searching || !query.trim()}
            aria-label="Search"
          >
            {searching ? <span className="spinner" /> : "→"}
          </button>
        </form>
        {searchError ? <p className="sheet-error">{searchError}</p> : null}
        {fdcDown ? (
          <p className="subsheet-hint">
            Food database unreachable — showing your foods only.
          </p>
        ) : null}
        {results ? (
          <div className="search-results">
            {results.map((r, i) => (
              <button
                key={`${r.source}:${r.source_id}:${i}`}
                type="button"
                disabled={opening}
                onClick={() => openHit(r)}
              >
                <span className={`result-name${r.food_id ? " food-name" : ""}`}>
                  {r.label}
                </span>
                <span className="result-sub">
                  {r.brand ? `${r.brand} · ` : ""}
                  {r.food_id ? "my foods" : (r.data_type ?? r.source)}
                </span>
              </button>
            ))}
            {results.length === 0 && !searching && !searchError ? (
              <p className="subsheet-hint">No matches — try fewer words.</p>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="m3-section lib-meals" aria-label="Saved meals">
        <h2>
          Saved meals
          <span className="count num">{meals ? activeMeals.length : ""}</span>
        </h2>
        {meals == null ? (
          <p className="m3-empty">Loading…</p>
        ) : meals.length === 0 ? (
          <p className="m3-empty">
            No saved meals yet. Say “save this as my usual breakfast” after
            logging, or tap <em>Save as meal</em> on a meal group.
          </p>
        ) : null}
        {activeMeals.map((m) => (
          <MealCard key={m.id} meal={m} onOpen={() => setMealSheet(m)} />
        ))}
        {inactiveMeals.map((m) => (
          <MealCard key={m.id} meal={m} onOpen={() => setMealSheet(m)} />
        ))}
        <button
          type="button"
          className="m3-link-btn"
          style={{ alignSelf: "flex-start" }}
          onClick={() => setNewMeal(true)}
        >
          + Build a meal
        </button>
      </section>

      <section className="m3-section lib-foods" aria-label="Your foods">
        <h2>
          Your foods
          <span className="count num">
            {aliases ? `${aliases.length} phrase${aliases.length === 1 ? "" : "s"}` : ""}
          </span>
        </h2>
        {aliases == null ? (
          <p className="m3-empty">Loading…</p>
        ) : groups.length === 0 ? (
          <p className="m3-empty">
            Nothing learned yet. Every meal you log teaches the app a phrase →
            food mapping; they show up here with use counts.
          </p>
        ) : null}
        {groups.map((g) => (
          <div className="m3-card" key={g.food_id}>
            <div className="m3-card-head">
              <div className="m3-card-title food-name">
                {/* prefetch off: this list is unbounded, and /library/food/[id]
                    is a dynamic auth-gated route, so the default viewport
                    prefetch would burn a middleware invocation per learned food
                    that scrolls past. The drill-in is a deliberate tap, not a
                    likely next navigation. */}
                <Link
                  href={`/library/food/${encodeURIComponent(g.food_id)}`}
                  prefetch={false}
                >
                  {g.food_name}
                </Link>
              </div>
              <span className="m3-card-stat num">{g.total}×</span>
            </div>
            {g.brand ? <div className="m3-card-sub">{g.brand}</div> : null}
            <div className="chips">
              {g.aliases.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="chip"
                  onClick={() => setAliasSheet(a)}
                >
                  {a.pinned ? <span className="usual-dot" /> : null}
                  {a.alias_norm}
                  <span className="alias-count num"> {a.use_count}×</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </section>

      {aliasSheet ? (
        <LibraryAliasSheet
          alias={aliasSheet}
          onSaved={(u) => {
            setAliases((prev) => (prev ?? []).map((a) => (a.id === u.id ? u : a)));
            setAliasSheet(null);
          }}
          onDeleted={(id) => {
            setAliases((prev) => (prev ?? []).filter((a) => a.id !== id));
            setAliasSheet(null);
          }}
          onClose={() => setAliasSheet(null)}
        />
      ) : null}

      {mealSheet || newMeal ? (
        <LibraryMealSheet
          meal={mealSheet}
          onSaved={(u) => {
            setMeals((prev) => {
              const list = prev ?? [];
              return list.some((m) => m.id === u.id)
                ? list.map((m) => (m.id === u.id ? u : m))
                : [u, ...list];
            });
            setMealSheet(null);
            setNewMeal(false);
          }}
          onClose={() => {
            setMealSheet(null);
            setNewMeal(false);
          }}
        />
      ) : null}
    </main>
  );
}
