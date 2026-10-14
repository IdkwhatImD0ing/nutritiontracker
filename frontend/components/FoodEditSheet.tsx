"use client";

import { useEffect, useState } from "react";
import {
  getAliasesForPhrase,
  type AliasRow,
  type StagedItem,
} from "@/lib/api";
import { foodName } from "@/lib/labels";
import SheetHeader from "./SheetHeader";

type Props = {
  item: StagedItem;
  /** true while a pick/re-resolve round trip for this row is in flight */
  busy: boolean;
  /** tap one of his aliases → resolves locally + records the explicit choice
   *  (§3.2 rule 2: a correction flips the very next default) */
  onPickAlias: (a: AliasRow) => void;
  /** re-resolve edited text through POST /api/resolve with the log context */
  onRephrase: (text: string) => void;
  /** escape hatch → the existing manual-search sheet (BACKLOG 3) */
  onManualSearch: () => void;
  onClose: () => void;
};

/** His cached foods for this phrase, read off the staged row: the current
 *  food plus every food_id-bearing alternate (tier-3 alternates carry
 *  source ids instead — those are database hits, not his aliases, and the
 *  row's own chips already offer them). Empty unless there is a real choice. */
function aliasesFromItem(item: StagedItem): AliasRow[] {
  const rivals = (item.alternates ?? []).filter(
    (a): a is typeof a & { food_id: string } =>
      !!a.food_id && a.food_id !== item.food?.id,
  );
  if (!rivals.length) return [];
  const current: AliasRow[] = item.food
    ? [
        {
          id: item.food.id,
          food_id: item.food.id,
          food_name: item.food.name,
          brand: item.food.brand,
        },
      ]
    : [];
  return [
    ...current,
    ...rivals.map((a) => ({
      id: a.food_id,
      food_id: a.food_id,
      food_name: a.label,
      pinned: a.usual,
    })),
  ];
}

/** §7.1 food bullet (BACKLOG 23): tap the food name → this sheet. Three ways
 *  out, fastest first: one of his own aliases for the phrase (one tap), a
 *  re-resolving text field, or manual database search. */
export default function FoodEditSheet({
  item,
  busy,
  onPickAlias,
  onRephrase,
  onManualSearch,
  onClose,
}: Props) {
  const [text, setText] = useState(item.phrase);
  const [aliases, setAliases] = useState<AliasRow[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    // GET /api/aliases?phrase= is a §5 seam — until the route lands, fall
    // back to what the staged payload already carries: tier 1's alternates
    // ARE his other alias rows for this phrase (§3.2, food_id-bearing), so
    // the section still renders the usual + its rivals from existing data.
    getAliasesForPhrase(item.phrase)
      .then((rows) => {
        if (!cancelled) setAliases(rows.length ? rows : aliasesFromItem(item));
      })
      .catch(() => {
        if (!cancelled) setAliases(aliasesFromItem(item));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Edit food">
        <SheetHeader title={<>&ldquo;{item.phrase}&rdquo;</>} onClose={onClose} />

        {aliases?.length ? (
          <div>
            <div className="edit-label">Your foods for this</div>
            <div className="chips">
              {aliases.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className={`chip${
                    a.food_id === item.food?.id ? " active" : ""
                  }`}
                  disabled={busy}
                  onClick={() => onPickAlias(a)}
                >
                  {a.pinned ? <span className="usual-dot" /> : null}
                  {foodName(a.food_name)}
                  {a.brand ? ` — ${a.brand}` : ""}
                  {typeof a.use_count === "number" ? (
                    <span className="alias-count num"> {a.use_count}×</span>
                  ) : null}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <form
          className="capture"
          onSubmit={(e) => {
            e.preventDefault();
            const trimmed = text.trim();
            if (trimmed && !busy) onRephrase(trimmed);
          }}
        >
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Describe it differently"
            aria-label="Re-resolve food"
            autoComplete="off"
          />
          <button type="submit" disabled={busy || !text.trim()} aria-label="Resolve">
            →
          </button>
        </form>

        <button
          type="button"
          className="edit-manual-btn"
          disabled={busy}
          onClick={onManualSearch}
        >
          Search the food database…
        </button>
      </div>
    </>
  );
}
