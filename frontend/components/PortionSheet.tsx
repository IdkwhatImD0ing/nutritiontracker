"use client";

import { useMemo, useState } from "react";
import type { StagedItem } from "@/lib/api";
import SheetHeader from "./SheetHeader";

/** A serving option normalized from the staged payload (§7.1 portion sheet). */
export type PortionChoice = {
  id: string;
  description: string;
  /** per-single-serving macros when the payload carries them */
  macros?: Record<string, number> | null;
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

  const qty = Number.parseFloat(qtyText);
  const valid = Number.isFinite(qty) && qty > 0;
  const chosen = servings.find((s) => s.id === servingId) ?? servings[0];

  const step = (d: number) => {
    const base = valid ? qty : 1;
    setQtyText(fmtQty(Math.max(0.25, base + d)));
  };

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
          <button type="button" aria-label="Less" onClick={() => step(-0.5)}>
            −
          </button>
          <input
            value={qtyText}
            onChange={(e) => setQtyText(e.target.value)}
            inputMode="decimal"
            aria-label="Quantity"
          />
          <button type="button" aria-label="More" onClick={() => step(0.5)}>
            +
          </button>
        </div>

        <div className="serving-list">
          {servings.map((s) => (
            <button
              key={s.id}
              type="button"
              className={s.id === (chosen?.id ?? "") ? "active" : undefined}
              onClick={() => setServingId(s.id)}
            >
              {s.description}
            </button>
          ))}
          {servings.length === 0 ? (
            <p className="subsheet-hint">No serving data for this item yet.</p>
          ) : null}
        </div>

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
