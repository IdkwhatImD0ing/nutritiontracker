"use client";

import { useId, useMemo, useState } from "react";
import type { StagedItem } from "@/lib/api";
import { fmt0, withUnit } from "@/lib/format";
import { isGramServing, rescaleQty, stepQty } from "./portion";
import SheetHeader from "./SheetHeader";

/** A serving option normalized from the staged payload (§7.1 portion sheet). */
export type PortionChoice = {
  id: string;
  description: string;
  /** per-single-serving macros when the payload carries them */
  macros?: Record<string, number> | null;
  /** one serving's weight, when the payload carries it */
  grams?: number | null;
};

type Props = {
  item: StagedItem;
  onApply: (serving: PortionChoice, qty: number) => void;
  onClose: () => void;
};

function fmtQty(q: number): string {
  return String(Math.round(q * 100) / 100);
}

/** §7.1: tap portion → qty stepper + the food's serving list. Fixing a wrong
 *  portion is ≤3 taps: open (tap portion) → adjust (tap +/− or a serving) →
 *  Done. */
export default function PortionSheet({ item, onApply, onClose }: Props) {
  const servings = useMemo<PortionChoice[]>(() => {
    const fromPayload = (item.servings ?? [])
      .map((s) => ({
        id: s.serving_id,
        description: s.description,
        macros: s.macros,
        grams: s.grams,
      }))
      .filter((s) => s.id);
    if (fromPayload.length) return fromPayload;
    // Fallback (older staged payloads without a serving list): the chosen
    // serving alone, so the qty stepper still works.
    return item.serving
      ? [{ id: item.serving.id, description: item.serving.description }]
      : [];
  }, [item]);

  const [servingId, setServingId] = useState(
    item.serving?.id ?? servings[0]?.id ?? "",
  );
  const [qtyText, setQtyText] = useState(fmtQty(item.qty ?? 1));
  const captionId = useId();

  const qty = Number.parseFloat(qtyText);
  const valid = Number.isFinite(qty) && qty > 0;
  const chosen = servings.find((s) => s.id === servingId) ?? servings[0];

  const step = (dir: 1 | -1) => {
    const base = valid ? qty : 1;
    setQtyText(fmtQty(stepQty(base, dir, chosen?.grams, 0.5)));
  };

  // Another serving counts the same food, so the number is converted with it
  // ("0.5 × 1 cup" becomes 74 g, not 0.5 g) when both weights are known.
  const pick = (s: PortionChoice) => {
    if (s.id === chosen?.id) return;
    const next = valid && chosen ? rescaleQty(qty, chosen.grams, s.grams) : null;
    if (next != null) setQtyText(fmtQty(next));
    setServingId(s.id);
  };

  // What the number counts, under it: "× 1 slice (32 g) = 64 g". A one-gram
  // serving is its own weight, so it is just "grams".
  const gramServing = isGramServing(chosen?.grams);
  const totalGrams =
    valid && chosen?.grams != null && !gramServing ? qty * chosen.grams : null;

  // Live kcal preview: per-serving macros when known, else rescale the row's
  // current display macros (same serving only — display-only either way; the
  // commit snapshot always re-derives server-side from the foods cache).
  let kcal: number | null = null;
  if (valid && chosen) {
    if (typeof chosen.macros?.kcal === "number") {
      kcal = chosen.macros.kcal * qty;
    } else if (
      item.serving?.id === chosen.id &&
      item.macros != null &&
      item.qty != null &&
      item.qty > 0
    ) {
      kcal = (item.macros.kcal / item.qty) * qty;
    }
  }

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Portion">
        <SheetHeader
          title={<span className="food-name">{item.food?.name ?? item.phrase}</span>}
          onClose={onClose}
        />

        <div className="stepper num">
          <button type="button" aria-label="Less" onClick={() => step(-1)}>
            −
          </button>
          <input
            value={qtyText}
            onChange={(e) => setQtyText(e.target.value)}
            inputMode="decimal"
            aria-label="Quantity"
            aria-describedby={chosen ? captionId : undefined}
          />
          <button type="button" aria-label="More" onClick={() => step(1)}>
            +
          </button>
        </div>
        {chosen ? (
          <p className="portion-caption num" id={captionId}>
            {gramServing ? "grams" : `× ${chosen.description}`}
            {totalGrams != null ? ` = ${withUnit(fmt0.format(totalGrams), "g")}` : ""}
          </p>
        ) : null}

        {/* A lone serving is not a choice: the caption above names it, and
            the list is for two or more. */}
        {servings.length > 1 ? (
          <div className="serving-list">
            {servings.map((s) => (
              <button
                key={s.id}
                type="button"
                className={s.id === chosen?.id ? "active" : undefined}
                aria-pressed={s.id === chosen?.id}
                onClick={() => pick(s)}
              >
                {s.description}
              </button>
            ))}
          </div>
        ) : servings.length === 0 ? (
          <p className="subsheet-hint">No serving data for this item yet.</p>
        ) : null}

        <button
          type="button"
          className="commit-btn"
          disabled={!valid || !chosen}
          onClick={() => {
            if (valid && chosen) onApply(chosen, Math.round(qty * 100) / 100);
          }}
        >
          <span className="num">
            Done{kcal != null ? ` · ${Math.round(kcal)} kcal` : ""}
          </span>
        </button>
      </div>
    </>
  );
}
