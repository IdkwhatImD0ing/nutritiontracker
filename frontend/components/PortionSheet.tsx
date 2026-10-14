"use client";

import { useState } from "react";
import { Check } from "./icons";
import SheetHeader from "./SheetHeader";

/** A serving option normalized from the staged payload (§7.1 portion sheet). */
export type PortionChoice = {
  id: string;
  description: string;
  /** per-single-serving macros when the payload carries them */
  macros?: Record<string, number> | null;
};

type Props = {
  /** the food's name, as the row shows it */
  title: string;
  /** null while the caller fetches them */
  servings: PortionChoice[] | null;
  initialServingId: string | null;
  initialQty: number;
  /** the current portion's kcal: previews the initial serving when it carries
   *  no macros of its own */
  initialKcal?: number | null;
  /** a serving the caller can't take (§4: partial macros) — listed, so the
   *  list is the food's real one, but not pickable */
  isDisabled?: (s: PortionChoice) => boolean;
  /** the caller's refusal, shown in place; the sheet stays open on it */
  error?: string | null;
  /** the caller's request is in flight: the sheet holds until it answers,
   *  so a refusal has somewhere to show */
  busy?: boolean;
  onApply: (serving: PortionChoice, qty: number) => void;
  onClose: () => void;
};

function fmtQty(q: number): string {
  return String(Math.round(q * 100) / 100);
}

/** §7.1: tap portion → qty stepper + the food's serving list. Fixing a wrong
 *  portion is ≤3 taps: open (tap portion) → adjust (tap +/− or a serving) →
 *  Done. The one portion editor: a staged row and a diary row both open it. */
export default function PortionSheet({
  title,
  servings,
  initialServingId,
  initialQty,
  initialKcal,
  isDisabled,
  error,
  busy = false,
  onApply,
  onClose,
}: Props) {
  const [servingId, setServingId] = useState(initialServingId ?? "");
  const [qtyText, setQtyText] = useState(fmtQty(initialQty));

  const list = servings ?? [];
  const qty = Number.parseFloat(qtyText);
  const valid = Number.isFinite(qty) && qty > 0;
  const chosen = list.find((s) => s.id === servingId) ?? list[0];

  const step = (d: number) => {
    const base = valid ? qty : 1;
    setQtyText(fmtQty(Math.max(0.25, base + d)));
  };

  // Live kcal preview: per-serving macros when known, else rescale the
  // current portion's kcal (same serving only — display-only either way;
  // what is stored is always re-derived server-side).
  let kcal: number | null = null;
  if (valid && chosen) {
    if (typeof chosen.macros?.kcal === "number") {
      kcal = chosen.macros.kcal * qty;
    } else if (
      chosen.id === initialServingId &&
      initialKcal != null &&
      initialQty > 0
    ) {
      kcal = (initialKcal / initialQty) * qty;
    }
  }

  return (
    <>
      <div
        className="subsheet-backdrop"
        onClick={busy ? undefined : onClose}
      />
      <div className="subsheet" role="dialog" aria-label="Portion">
        <SheetHeader title={title} onClose={onClose} canClose={!busy} />

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
          {list.map((s) => {
            const off = isDisabled?.(s) ?? false;
            const active = s.id === (chosen?.id ?? "");
            return (
              <button
                key={s.id}
                type="button"
                className={active ? "active" : undefined}
                disabled={off}
                title={
                  off
                    ? "This serving is missing macros, so it can't be logged."
                    : undefined
                }
                onClick={() => setServingId(s.id)}
              >
                <span>
                  {s.description}
                  {off ? " · no macros" : ""}
                </span>
                {active ? <Check /> : null}
              </button>
            );
          })}
          {servings == null ? (
            <p className="subsheet-hint">Loading servings…</p>
          ) : servings.length === 0 ? (
            <p className="subsheet-hint">No serving data for this item yet.</p>
          ) : null}
        </div>

        {error ? <p className="sheet-error">{error}</p> : null}

        <button
          type="button"
          className="commit-btn"
          disabled={busy || !valid || !chosen}
          onClick={() => {
            if (valid && chosen) onApply(chosen, Math.round(qty * 100) / 100);
          }}
        >
          <span className="num">
            {busy
              ? "Saving…"
              : `Done${kcal != null ? ` · ${Math.round(kcal)} kcal` : ""}`}
          </span>
        </button>
      </div>
    </>
  );
}
