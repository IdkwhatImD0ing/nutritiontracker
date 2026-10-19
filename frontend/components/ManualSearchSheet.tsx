"use client";

import { useEffect, useRef, useState } from "react";
import {
  quotaRefusal,
  recentFoods,
  searchFoods,
  type FoodSearchResult,
} from "@/lib/api";
import { fmt0, withUnit } from "@/lib/format";
import { foodName } from "@/lib/labels";
import { hitSub } from "@/lib/sources";
import ErrorNotice from "./ErrorNotice";
import { FOOD_FIELD_PROPS } from "./fieldProps";
import { ArrowRight, Close } from "./icons";
import SheetHeader, { SheetClose } from "./SheetHeader";
import Skeleton from "./Skeleton";
import { useSheetBack } from "./sheetBack";
import { useSheetFocus } from "./useSheetFocus";

type Props = {
  /** the row's phrase (pre-searched); "" = the + sheet's standalone Search
   *  lane — nothing prefilled, the field takes focus */
  phrase: string;
  /** true while the parent's food-select round trip is in flight */
  busy: boolean;
  /** the parent's select failure, rendered above the results */
  error?: string | null;
  /** a pick met the demo quota's 429: every result would meet it again, so
   *  none takes a tap (searching itself is free) */
  refused?: boolean;
  /** the + sheet's Search lane: the whole screen (owner ruling, 2026-09-06),
   *  not the sub-sheet a staged row opens over the staging sheet */
  fullScreen?: boolean;
  onSelect: (r: FoodSearchResult) => void;
  onClose: () => void;
};

/** " · 120 kcal · 10 g protein / 100 g" (per 100 ml for a drink): what tells
 *  look-alike rows apart. Calories are a row's headline (§7.1), so protein
 *  shows only beside them; "" when the source gave no per-100 figures. */
function per100(r: FoodSearchResult): string {
  if (r.kcal_per_100g == null) return "";
  const protein =
    r.protein_per_100g == null
      ? ""
      : ` · ${withUnit(fmt0.format(r.protein_per_100g), "g")} protein`;
  const basis = withUnit("100", r.per_100ml ? "ml" : "g");
  return ` · ${withUnit(fmt0.format(r.kcal_per_100g), "kcal")}${protein} / ${basis}`;
}

/** BACKLOG 3 / §3.4: `not_found` flips the row to a manual search box — a
 *  plain search UI over GET /api/foods/search, each row naming its calories
 *  and protein per 100 g (owner ruling, 2026-10-13). The same
 *  component is the + sheet's Search lane (owner ruling, 2026-09-02) — full
 *  screen since 2026-09-06, with the foods you have logged before listed
 *  under the bar until a search runs (GET /api/foods/recent, newest first,
 *  200 at most; a tap logs one again). */
export default function ManualSearchSheet({
  phrase,
  busy,
  error,
  refused = false,
  fullScreen = false,
  onSelect,
  onClose,
}: Props) {
  // focus in and back out; also what useSheetBack ranks (full screen)
  const sheetRef = useSheetFocus<HTMLDivElement>();
  const [query, setQuery] = useState(phrase);
  const [results, setResults] = useState<FoodSearchResult[] | null>(null);
  const [recent, setRecent] = useState<FoodSearchResult[] | null>(null);
  const [recentFailed, setRecentFailed] = useState(false);
  const [fdcDown, setFdcDown] = useState(false);
  // A row's phrase is searched on mount: searching from the first paint, so
  // the recent list's skeleton never flashes before the results' one.
  const [searching, setSearching] = useState(() => phrase.trim() !== "");
  // `retry` is false for a demo 429: a second try meets the same refusal
  const [searchError, setSearchError] = useState<{
    text: string;
    retry: boolean;
  } | null>(null);
  const seq = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);
  // The tapped row (`source:source_id`): it keeps a spinner while the
  // parent's select runs; the rest stay readable but can't be tapped.
  const [picked, setPicked] = useState<string | null>(null);
  const pickedKey = busy ? picked : null;

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
      const quota = quotaRefusal(e);
      setSearchError(
        quota
          ? { text: quota.detail, retry: false }
          : { text: "Search failed.", retry: true },
      );
    } finally {
      if (seq.current === id) setSearching(false);
    }
  };

  // The field emptied — by its ✕ or by hand: the search is over, so an answer
  // still in flight and the last one's notices go with it, and "Previously
  // added" comes back.
  const reset = () => {
    seq.current++;
    setResults(null);
    setSearching(false);
    setSearchError(null);
    setFdcDown(false);
  };

  // Pre-run the search with the row's phrase — one fewer tap.
  useEffect(() => {
    void run(phrase);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The previously logged foods, once per open. A failed read says so in one
  // quiet line, never the first-run copy — the list is a convenience, search
  // still works.
  useEffect(() => {
    let cancelled = false;
    recentFoods()
      .then((r) => {
        if (!cancelled) setRecent(r);
      })
      .catch(() => {
        if (!cancelled) setRecentFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Escape and Back: the sub-sheet's come through its SheetHeader
  useSheetBack(sheetRef, onClose, fullScreen);

  // Until a search has run — or once the field is cleared again — the
  // previously logged foods take the results' place. A pre-searched row
  // phrase shows nothing while its search is in flight, never a flash of
  // unrelated foods.
  const showRecent = !query.trim() || (results == null && !searching);

  // Cached foods (food_id set) carry the adapters' lowercase display names and
  // a raw FDC description can be ALL CAPS; foodName() gives both one casing,
  // the text itself unchanged.
  const hit = (r: FoodSearchResult, i: number) => (
    <button
      key={`${r.source}:${r.source_id}:${i}`}
      type="button"
      className={refused ? "refused" : undefined}
      disabled={busy || refused}
      aria-busy={pickedKey === `${r.source}:${r.source_id}` || undefined}
      onClick={() => {
        setPicked(`${r.source}:${r.source_id}`);
        onSelect(r);
      }}
    >
      <span className="result-name">{foodName(r.label)}</span>
      <span className="result-sub">
        {hitSub(r)}
        {per100(r)}
      </span>
      {pickedKey === `${r.source}:${r.source_id}` ? <span className="spinner" /> : null}
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
        <div className="capture-field">
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              if (!e.target.value.trim()) reset();
            }}
            placeholder="Search foods"
            aria-label="Search foods"
            autoComplete="off"
            autoFocus={!phrase}
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
        <button type="submit" disabled={searching || !query.trim()} aria-label="Search">
          {searching ? (
            <span className="spinner" />
          ) : (
            <ArrowRight strokeWidth={2.4} />
          )}
        </button>
      </form>

      {error ? <ErrorNotice message={error} /> : null}
      {searchError ? (
        <ErrorNotice
          message={searchError.text}
          onRetry={searchError.retry ? () => void run(query) : undefined}
        />
      ) : null}
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
          ) : recentFailed ? (
            <p className="subsheet-hint">
              Couldn&rsquo;t load your recent foods. Search still works.
            </p>
          ) : (
            <Skeleton rows={5} height={52} />
          )}
        </div>
      ) : (
        <div className="search-results">
          {searching && results == null ? <Skeleton rows={4} height={52} /> : null}
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
        <div
          ref={sheetRef}
          className="sheet search-sheet"
          role="dialog"
          aria-modal="true"
          aria-label="Search foods"
          tabIndex={-1}
        >
          <div className="sheet-head">
            <div className="sheet-head-row">
              <h2 className="sheet-title">Search</h2>
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
      <div
        ref={sheetRef}
        className="subsheet"
        role="dialog"
        aria-modal="true"
        aria-label="Search foods"
        tabIndex={-1}
      >
        <SheetHeader title="Search" onClose={onClose} />
        {body}
      </div>
    </>
  );
}
