"use client";

import { useId, useState } from "react";
import { fmt0, withUnit } from "@/lib/format";
import { Check } from "./icons";
import { isGramServing, rescaleQty, stepQty } from "./portion";
import SheetHeader from "./SheetHeader";
import { useSheetFocus } from "./useSheetFocus";

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
  const sheetRef = useSheetFocus<HTMLDivElement>();
  const [servingId, setServingId] = useState(initialServingId ?? "");
  const [qtyText, setQtyText] = useState(fmtQty(initialQty));
  const captionId = useId();

  const list = servings ?? [];
  const qty = Number.parseFloat(qtyText);
  const valid = Number.isFinite(qty) && qty > 0;
  const chosen = list.find((s) => s.id === servingId) ?? list[0];

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
      <div
        ref={sheetRef}
        className="subsheet"
        role="dialog"
        aria-modal="true"
        aria-label="Portion"
        tabIndex={-1}
      >
        <SheetHeader title={title} onClose={onClose} canClose={!busy} />

        <div className="stepper num">
          <button type="button" aria-label="Decrease quantity" onClick={() => step(-1)}>
            −
          </button>
          <input
            value={qtyText}
            onChange={(e) => setQtyText(e.target.value)}
            inputMode="decimal"
            aria-label="Quantity"
            aria-describedby={chosen ? captionId : undefined}
          />
          <button type="button" aria-label="Increase quantity" onClick={() => step(1)}>
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
        {servings == null ? (
          <p className="subsheet-hint">Loading servings…</p>
        ) : list.length > 1 ? (
          <div className="serving-list">
            {list.map((s) => {
              const off = isDisabled?.(s) ?? false;
              const active = s.id === (chosen?.id ?? "");
              return (
                <button
                  key={s.id}
                  type="button"
                  className={active ? "active" : undefined}
                  aria-pressed={active}
                  disabled={off}
                  title={
                    off
                      ? "This serving is missing macros, so it can't be logged."
                      : undefined
                  }
                  onClick={() => pick(s)}
                >
                  <span>
                    {s.description}
                    {off ? " · no macros" : ""}
                  </span>
                  {active ? <Check /> : null}
                </button>
              );
            })}
          </div>
        ) : list.length === 0 ? (
          <p className="subsheet-hint">No serving data for this item yet.</p>
        ) : null}

        <div className="subsheet-foot">
          {error ? (
            <p className="sheet-error" role="alert">
              {error}
            </p>
          ) : null}

          <button
            type="button"
            className="commit-btn"
            // busy waits as aria-disabled, not disabled: a disabled button
            // drops focus to the page behind the sheet, and a refusal
            // appears right above it
            disabled={!valid || !chosen}
            aria-disabled={busy || undefined}
            aria-busy={busy}
            onClick={() => {
              if (!busy && valid && chosen) onApply(chosen, Math.round(qty * 100) / 100);
            }}
          >
            {busy ? <span className="spinner on-accent" /> : null}
            <span className="num">
              {busy
                ? "Saving…"
                : `Done${kcal != null ? ` · ${Math.round(kcal)} kcal` : ""}`}
            </span>
          </button>
        </div>
      </div>
    </>
  );
}
