"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { searchFoods, type FoodSearchResult } from "@/lib/api";
import { fmt0 } from "@/lib/format";
import { foodName } from "@/lib/labels";
import { hitSub } from "@/lib/sources";
import ErrorNotice from "./ErrorNotice";
import { FOOD_FIELD_PROPS } from "./fieldProps";
import { ArrowRight, Close } from "./icons";
import {
  cacheFood,
  getAliases,
  getMeals,
  type AliasRow,
  type SavedMeal,
} from "./LibraryApi";
import LibraryAliasSheet from "./LibraryAliasSheet";
import LibraryMealSheet from "./LibraryMealSheet";
import { peek, put } from "./readCache";
import { useScrollMemory } from "./useScrollMemory";
import Skeleton from "./Skeleton";
import { RESUME_STALE_MS, useOnForeground } from "./useOnForeground";
import { OFFLINE_NOTE, useOnline } from "./useOnline";
import SheetPresence from "./useSheetPresence";

type FoodGroup = {
  food_id: string;
  food_name: string;
  brand: string | null;
  total: number;
  aliases: AliasRow[];
};

/** §7.4 Library: his phrases (aliases) grouped by food, most-used first. */
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

type LastSearch = { q: string; hits: FoodSearchResult[]; fdcDown: boolean };

/** The last search's answer while the URL still asks for it (?q=): a Back
 *  from a food it opened paints the results with the lists, at full height,
 *  so useScrollMemory puts the page back over them. readCache is empty on
 *  the server and on a hard load, so hydration never sees one. */
function lastSearch(): LastSearch | null {
  const c = peek<LastSearch>("library:search");
  if (!c) return null;
  return new URLSearchParams(window.location.search).get("q") === c.q ? c : null;
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
          : "No phrases yet"}
      </span>
    </button>
  );
}

export default function Library() {
  const router = useRouter();
  // A return to the tab paints the lists it last showed (readCache) while
  // the read below refreshes them.
  const [aliases, setAliases] = useState<AliasRow[] | null>(() =>
    peek<AliasRow[]>("library:aliases"),
  );
  const [meals, setMeals] = useState<SavedMeal[] | null>(() =>
    peek<SavedMeal[]>("library:meals"),
  );
  // Which read failed. A failed list stays null rather than [], so its
  // section says it couldn't load instead of "0 foods" and the first-run copy.
  const [failed, setFailed] = useState({ aliases: false, meals: false });

  const [query, setQuery] = useState(() => lastSearch()?.q ?? "");
  const [results, setResults] = useState<FoodSearchResult[] | null>(
    () => lastSearch()?.hits ?? null,
  );
  const [fdcDown, setFdcDown] = useState(() => lastSearch()?.fdcDown ?? false);
  const [searching, setSearching] = useState(false);
  // the hit being opened (`source:source_id`), marked with a spinner
  const [opening, setOpening] = useState<string | null>(null);
  // `retry`: a failed search re-runs; a failed open is tapped again instead
  const [searchError, setSearchError] = useState<{
    text: string;
    retry: boolean;
  } | null>(null);
  const seq = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  // A section's Retry swaps its notice for a skeleton: focus waits on the
  // section's heading instead of falling to the top of the page.
  const mealsHead = useRef<HTMLHeadingElement>(null);
  const phrasesHead = useRef<HTMLHeadingElement>(null);

  const [aliasSheet, setAliasSheet] = useState<AliasRow | null>(null);
  const [mealSheet, setMealSheet] = useState<SavedMeal | null>(null);
  /** the same sheet with no meal = the §8.4 "build one in Library" path */
  const [newMeal, setNewMeal] = useState(false);
  const online = useOnline();

  // `attempt` re-runs both reads: each section's failure notice carries a
  // Retry rather than asking for a page reload.
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    // What this render shows as the read starts. A resume's re-read over
    // lists already shown keeps them when it fails, as Today's tracker keeps
    // its numbers; only a list with nothing on screen says it couldn't load.
    const shown = { aliases: aliases != null, meals: meals != null };
    Promise.allSettled([getAliases(), getMeals()]).then(([a, m]) => {
      if (cancelled) return;
      if (a.status === "fulfilled") setAliases(a.value);
      if (m.status === "fulfilled") setMeals(m.value);
      setFailed({
        aliases: a.status === "rejected" && !shown.aliases,
        meals: m.status === "rejected" && !shown.meals,
      });
    });
    return () => {
      cancelled = true;
    };
  }, [attempt]);
  // A failed list is already null, so clearing `failed` shows its skeleton;
  // the list that loaded stays on screen.
  const retry = () => {
    setFailed({ aliases: false, meals: false });
    setAttempt((n) => n + 1);
  };
  // Back after a while (installed, nothing else re-reads): the same reads,
  // over the lists already shown, so nothing flashes back to loading.
  useOnForeground((away) => {
    if (away >= RESUME_STALE_MS) setAttempt((n) => n + 1);
  });

  // Written through, so an alias edit or a saved meal is in the next paint too.
  useEffect(() => {
    if (aliases) put("library:aliases", aliases);
  }, [aliases]);
  useEffect(() => {
    if (meals) put("library:meals", meals);
  }, [meals]);
  useScrollMemory(aliases !== null && meals !== null);

  const groups = useMemo(() => groupByFood(aliases ?? []), [aliases]);
  const activeMeals = (meals ?? []).filter((m) => m.is_active);
  const inactiveMeals = (meals ?? []).filter((m) => !m.is_active);

  // The submitted query rides in the URL (?q=), so "‹ Library" back from a
  // food — a fresh mount — runs it again instead of landing on an empty
  // field. replaceState rather than the router: no navigation, no new
  // history entry, and no useSearchParams Suspense on a prerendered page.
  const search = async (raw: string) => {
    const q = raw.trim();
    if (!q) return;
    const id = ++seq.current;
    // Not when the URL already says it: the mount's re-run on a hard load
    // comes before Next wraps replaceState, and the bare call would wipe
    // the router's state from this entry, so a later Back onto it leaves
    // the page above it on screen.
    if (new URLSearchParams(window.location.search).get("q")?.trim() !== q) {
      window.history.replaceState(null, "", `?q=${encodeURIComponent(q)}`);
    }
    setSearching(true);
    setSearchError(null);
    try {
      const r = await searchFoods(q);
      if (seq.current !== id) return;
      setResults(r.hits);
      setFdcDown(r.fdcError != null);
      put("library:search", { q, hits: r.hits, fdcDown: r.fdcError != null });
    } catch {
      if (seq.current !== id) return;
      // Hits on screen for this same query (a Back's restored list, a Retry
      // over it) stay, as the lists below keep theirs; another query's go,
      // leaving no results block at all: an empty one holds 60px of blank
      // under the error card. So does this query's "No matches": with the
      // error up its hint hides, and [] would be that empty block.
      const last = peek<LastSearch>("library:search");
      const keep = last?.q === q && last.hits.length > 0;
      setResults((r) => (r && keep ? r : null));
      // nor the last answer's "USDA didn't answer — showing…" over nothing;
      // kept hits keep the hint that explains them
      if (!keep) setFdcDown(false);
      setSearchError({ text: "Search failed.", retry: true });
    } finally {
      if (seq.current === id) setSearching(false);
    }
  };

  const runSearch = (e: FormEvent) => {
    e.preventDefault();
    void search(query);
  };

  // The field emptied — by its ✕ or by hand: the search is over. No old
  // results pinned above Saved meals, no late answer landing, no ?q= to
  // bring them back.
  const reset = () => {
    seq.current++;
    setResults(null);
    setSearching(false);
    setSearchError(null);
    setFdcDown(false);
    window.history.replaceState(null, "", window.location.pathname);
  };

  useEffect(() => {
    const q = new URLSearchParams(window.location.search).get("q");
    if (!q) return;
    setQuery(q);
    void search(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Manual search entry → food detail. FDC hits are cached first (the
  // existing /api/foods/select library path) so the detail page has a row.
  const openHit = async (hit: FoodSearchResult) => {
    setOpening(`${hit.source}:${hit.source_id}`);
    setSearchError(null);
    try {
      const id = await cacheFood(hit);
      router.push(`/library/food/${encodeURIComponent(id)}`);
    } catch {
      setSearchError({ text: "Couldn't open that food.", retry: false });
      setOpening(null);
    }
  };

  return (
    <main className="shell library">
      {/* no count here: Your phrases counts its phrases, and a second number
          counting something else beside it read as a mismatch */}
      <header className="brand">
        <h1>Library</h1>
      </header>

      {/* mounted online too, empty and out of the flow ([role=status]:empty),
          so the note is announced as it arrives, as on Today */}
      <div
        className={online ? undefined : "notice warn offline-note"}
        role="status"
      >
        {online ? null : <span>{OFFLINE_NOTE}</span>}
      </div>

      <section className="m3-section lib-search" aria-label="Look up a food">
        <form className="capture" onSubmit={runSearch}>
          <div className="capture-field">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                if (!e.target.value.trim()) reset();
              }}
              placeholder="Look up a food"
              aria-label="Look up a food"
              autoComplete="off"
              {...FOOD_FIELD_PROPS}
              enterKeyHint="search"
            />
            {query ? (
              <button
                type="button"
                className="field-clear"
                aria-label="Clear search"
                // keep the field focused, so the keyboard doesn't drop and rise
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  setQuery("");
                  reset();
                  inputRef.current?.focus();
                }}
              >
                <Close />
              </button>
            ) : null}
          </div>
          <button
            type="submit"
            disabled={searching || !query.trim()}
            aria-label="Search"
          >
            {searching ? (
              <span className="spinner" />
            ) : (
              <ArrowRight strokeWidth={2.4} />
            )}
          </button>
        </form>
        {searchError ? (
          <ErrorNotice
            message={searchError.text}
            onRetry={searchError.retry ? () => void search(query) : undefined}
          />
        ) : null}
        {fdcDown ? (
          <p className="subsheet-hint">
            USDA search didn&rsquo;t answer — showing foods already saved in
            Tally.
          </p>
        ) : null}
        {results ? (
          <div className="search-results">
            {results.map((r, i) => (
              <button
                key={`${r.source}:${r.source_id}:${i}`}
                type="button"
                disabled={opening != null}
                aria-busy={opening === `${r.source}:${r.source_id}` || undefined}
                onClick={() => openHit(r)}
              >
                <span className="result-name">{foodName(r.label)}</span>
                <span className="result-sub">{hitSub(r)}</span>
                {opening === `${r.source}:${r.source_id}` ? (
                  <span className="spinner" />
                ) : null}
              </button>
            ))}
            {results.length === 0 && !searching && !searchError ? (
              <p className="subsheet-hint">No matches — try fewer words.</p>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="m3-section lib-meals" aria-label="Saved meals">
        <h2 ref={mealsHead} tabIndex={-1}>
          Saved meals
          {activeMeals.length > 0 ? (
            <span className="count num">{activeMeals.length}</span>
          ) : null}
        </h2>
        {failed.meals ? (
          <ErrorNotice
            message="Couldn't load your saved meals."
            onRetry={() => {
              mealsHead.current?.focus();
              retry();
            }}
          />
        ) : meals == null ? (
          <Skeleton rows={2} height={64} />
        ) : meals.length === 0 ? (
          <p className="m3-empty">
            No saved meals yet. Say “save this as my usual breakfast” after
            logging, or tap <em>Save as meal</em> on a meal group.
          </p>
        ) : null}
        {/* one list, active first: a meal that is deactivated or
            reactivated keeps its card (moved, not remounted), so focus goes
            back to it as its sheet closes */}
        {[...activeMeals, ...inactiveMeals].map((m) => (
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

      {/* phrases, not every food (Bill, 2026-10-13): a barcode, search or
          photo log learns no alias (§3.5), so it never appears here */}
      <section className="m3-section lib-foods" aria-label="Your phrases">
        <h2 ref={phrasesHead} tabIndex={-1}>
          Your phrases
          {aliases?.length ? (
            <span className="count num">
              {`${aliases.length} phrase${aliases.length === 1 ? "" : "s"}`}
            </span>
          ) : null}
        </h2>
        {failed.aliases ? (
          <ErrorNotice
            message="Couldn't load your phrases."
            onRetry={() => {
              phrasesHead.current?.focus();
              retry();
            }}
          />
        ) : aliases == null ? (
          <Skeleton rows={2} height={64} />
        ) : groups.length === 0 ? (
          <p className="m3-empty">
            Nothing here yet. Foods you log by voice or by typing show up here
            with the words you used for them.
          </p>
        ) : null}
        {aliases?.some((a) => a.pinned) ? (
          <p className="m3-note">
            <span className="usual-dot" aria-hidden="true" />
            Pinned: always logs that food and portion
          </p>
        ) : null}
        {groups.map((g) => (
          <div className="m3-card" key={g.food_id}>
            {/* The whole upper card is the drill-in, not just the name text.
                prefetch off: this list is unbounded, and /library/food/[id]
                is a dynamic auth-gated route, so the default viewport
                prefetch would burn a middleware invocation per learned food
                that scrolls past. The drill-in is a deliberate tap, not a
                likely next navigation. */}
            <Link
              className="m3-card-link"
              href={`/library/food/${encodeURIComponent(g.food_id)}`}
              prefetch={false}
            >
              <div className="m3-card-head">
                <div className="m3-card-title">{foodName(g.food_name)}</div>
                {/* one phrase: its chip already shows this total */}
                {g.aliases.length > 1 ? (
                  <span className="m3-card-stat num">{fmt0.format(g.total)}×</span>
                ) : null}
                <span className="chev" aria-hidden="true" />
              </div>
              {g.brand ? <div className="m3-card-sub">{g.brand}</div> : null}
            </Link>
            <div className="chips">
              {g.aliases.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="chip"
                  onClick={() => setAliasSheet(a)}
                >
                  {/* the dot is the eye's; a screen reader hears the word */}
                  {a.pinned ? (
                    <>
                      <span className="usual-dot" aria-hidden="true" />
                      <span className="sr-only">Pinned: </span>
                    </>
                  ) : null}
                  {a.alias_norm}
                  <span className="alias-count num"> {fmt0.format(a.use_count)}×</span>
                </button>
              ))}
            </div>
          </div>
        ))}
      </section>

      <SheetPresence>
        {aliasSheet ? (
          <LibraryAliasSheet
            alias={aliasSheet}
            onSaved={(u) => {
              setAliases((prev) => (prev ?? []).map((a) => (a.id === u.id ? u : a)));
              setAliasSheet(null);
            }}
            onDeleted={(id) => {
              // the chip that opened the sheet goes with the phrase: focus
              // waits on the list's heading, not the top of the page. Not if
              // it has moved on (the sheet closed while the delete was out).
              const at = document.activeElement;
              if (!at || at === document.body || at.closest('[aria-modal="true"]')) {
                phrasesHead.current?.focus({ preventScroll: true });
              }
              setAliases((prev) => (prev ?? []).filter((a) => a.id !== id));
              setAliasSheet(null);
            }}
            onClose={() => setAliasSheet(null)}
          />
        ) : null}
      </SheetPresence>

      <SheetPresence>
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
      </SheetPresence>
    </main>
  );
}
