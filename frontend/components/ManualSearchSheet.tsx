"use client";

import { useEffect, useRef, useState } from "react";
import {
  quotaRefusal,
  recentFoods,
  searchFoods,
  type FoodSearchResult,
} from "@/lib/api";
import SheetHeader, { SheetClose } from "./SheetHeader";

type Props = {
  /** the row's phrase (pre-searched); "" = the + sheet's standalone Search
   *  lane — nothing prefilled, the field takes focus */
  phrase: string;
  /** true while the parent's food-select round trip is in flight */
  busy: boolean;
  /** the parent's select failure, rendered above the results */
  error?: string | null;
  /** the + sheet's Search lane: the whole screen (owner ruling, 2026-09-06),
   *  not the sub-sheet a staged row opens over the staging sheet */
  fullScreen?: boolean;
  onSelect: (r: FoodSearchResult) => void;
  onClose: () => void;
};

/** BACKLOG 3 / §3.4: `not_found` flips the row to a manual search box — a
 *  plain search UI over GET /api/foods/search, deliberately boring. The same
 *  component is the + sheet's Search lane (owner ruling, 2026-09-02) — full
 *  screen since 2026-09-06, with the foods you have logged before listed
 *  under the bar until a search runs (GET /api/foods/recent, newest first,
 *  200 at most; a tap logs one again). */
export default function ManualSearchSheet({
  phrase,
  busy,
  error,
  fullScreen = false,
  onSelect,
  onClose,
}: Props) {
  const [query, setQuery] = useState(phrase);
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [recent, setRecent] = useState<FoodSearchResult[] | null>(null);
  const [fdcDown, setFdcDown] = useState(false);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const seq = useRef(0);

  const run = async (q: string) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    const id = ++seq.current;
    setSearching(true);
    setSearchError(null);
    try {
      const r = await searchFoods(trimmed);
      if (seq.current !== id) return;
      setResults(r.hits);
      setFdcDown(r.fdcError != null);
    } catch (e) {
      if (seq.current !== id) return;
      setResults([]);
      // a demo 429 says why in its own words — show those, not the generic line
      setSearchError(quotaRefusal(e)?.detail ?? "Search failed. Try again.");
    } finally {
      if (seq.current === id) setSearching(false);
    }
  };

  // Pre-run the search with the row's phrase — one fewer tap.
  useEffect(() => {
    void run(phrase);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The previously logged foods, once per open. A failed read shows nothing
  // in their place — the list is a convenience, search still works.
  useEffect(() => {
    let cancelled = false;
    recentFoods()
      .then((r) => {
        if (!cancelled) setRecent(r);
      })
      .catch(() => {
        if (!cancelled) setRecent([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Until a search has run — or once the field is cleared again — the
  // previously logged foods take the results' place. A pre-searched row
  // phrase shows nothing while its search is in flight, never a flash of
  // unrelated foods.
  const showRecent = !query.trim() || (results == null && !searching);

  // Cached foods (food_id set) carry the adapters' lowercase display names;
  // .food-name capitalizes those. A raw FDC description is shown as-is.
  const hit = (r: FoodSearchResult, i: number) => (
    <button
      key={`${r.source}:${r.source_id}:${i}`}
      type="button"
      disabled={busy}
      onClick={() => onSelect(r)}
    >
      <span className={`result-name${r.food_id ? " food-name" : ""}`}>
        {r.label}
      </span>
      <span className="result-sub">
        {r.brand ? `${r.brand} · ` : ""}
        {r.food_id ? "my foods" : (r.data_type ?? r.source)}
      </span>
    </button>
  );

  const body = (
    <>
      <form
        className="capture"
        onSubmit={(e) => {
          e.preventDefault();
          void run(query);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search foods"
          aria-label="Search foods"
          autoComplete="off"
          autoFocus={!phrase}
        />
        <button type="submit" disabled={searching || !query.trim()} aria-label="Search">
          {searching ? <span className="spinner" /> : "→"}
        </button>
      </form>

      {error ? <p className="sheet-error">{error}</p> : null}
      {searchError ? <p className="sheet-error">{searchError}</p> : null}
      {fdcDown ? (
        <p className="subsheet-hint">
          Food database unreachable — showing your foods only.
        </p>
      ) : null}

      {showRecent ? (
        <div className="search-results" aria-label="Previously added">
          {recent != null && recent.length > 0 ? (
            <>
              <p className="search-label">Previously added</p>
              {recent.map(hit)}
            </>
          ) : recent != null ? (
            <p className="subsheet-hint">
              Foods you log show up here, newest first.
            </p>
          ) : null}
        </div>
      ) : (
        <div className="search-results">
          {(results ?? []).map(hit)}
          {results != null && results.length === 0 && !searching && !searchError ? (
            <p className="subsheet-hint">No matches — try fewer words.</p>
          ) : null}
        </div>
      )}
    </>
  );

  if (fullScreen) {
    return (
      <>
        <div className="sheet-backdrop" onClick={onClose} />
        <div className="sheet search-sheet" role="dialog" aria-label="Search foods">
          <div className="sheet-head">
            <div className="sheet-head-row">
              <span className="sheet-title">Search</span>
              <SheetClose onClick={onClose} />
            </div>
          </div>
          <div className="sheet-body search-body">{body}</div>
        </div>
      </>
    );
  }

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Manual food search">
        <SheetHeader title="Find a food" onClose={onClose} />
        {body}
      </div>
    </>
  );
}
