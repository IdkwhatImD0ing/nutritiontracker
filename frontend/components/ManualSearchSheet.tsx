"use client";

import { useEffect, useRef, useState } from "react";
import { searchFoods, type FoodSearchResult } from "@/lib/api";

type Props = {
  phrase: string;
  /** true while the parent's food-select round trip is in flight */
  busy: boolean;
  onSelect: (r: FoodSearchResult) => void;
  onClose: () => void;
};

/** BACKLOG 3 / §3.4: `not_found` flips the row to a manual search box — a
 *  plain search UI over GET /api/foods/search, deliberately boring. */
export default function ManualSearchSheet({
  phrase,
  busy,
  onSelect,
  onClose,
}: Props) {
  const [query, setQuery] = useState(phrase);
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [fdcDown, setFdcDown] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const run = async (q: string) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    const id = ++seq.current;
    setSearching(true);
    setError(null);
    try {
      const r = await searchFoods(trimmed);
      if (seq.current !== id) return;
      setResults(r.hits);
      setFdcDown(r.fdcError != null);
    } catch {
      if (seq.current !== id) return;
      setResults([]);
      setError("Search failed. Try again.");
    } finally {
      if (seq.current === id) setSearching(false);
    }
  };

  // Pre-run the search with the row's phrase — one fewer tap.
  useEffect(() => {
    void run(phrase);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Manual food search">
        <div className="subsheet-title">Find a food</div>

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
          />
          <button type="submit" disabled={searching || !query.trim()} aria-label="Search">
            {searching ? <span className="spinner" /> : "→"}
          </button>
        </form>

        {error ? <p className="sheet-error">{error}</p> : null}
        {fdcDown ? (
          <p className="subsheet-hint">
            Food database unreachable — showing your foods only.
          </p>
        ) : null}

        <div className="search-results">
          {(results ?? []).map((r, i) => (
            <button
              key={`${r.source}:${r.source_id}:${i}`}
              type="button"
              disabled={busy}
              onClick={() => onSelect(r)}
            >
              <span className="result-name">{r.label}</span>
              <span className="result-sub">
                {r.brand ? `${r.brand} · ` : ""}
                {r.food_id ? "my foods" : (r.data_type ?? r.source)}
              </span>
            </button>
          ))}
          {results != null && results.length === 0 && !searching && !error ? (
            <p className="subsheet-hint">No matches — try fewer words.</p>
          ) : null}
        </div>
      </div>
    </>
  );
}
