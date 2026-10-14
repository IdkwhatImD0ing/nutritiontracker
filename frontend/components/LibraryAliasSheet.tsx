"use client";

import { useEffect, useState } from "react";
import type { StagedServing } from "@/lib/api";
import { deleteAlias, getFood, patchAlias, type AliasRow } from "./LibraryApi";
import SheetHeader from "./SheetHeader";
import { useSheetFocus } from "./useSheetFocus";

type Props = {
  alias: AliasRow;
  onSaved: (a: AliasRow) => void;
  onDeleted: (id: string) => void;
  onClose: () => void;
};

function fmtQty(q: number): string {
  return String(Math.round(q * 100) / 100);
}

/** §7.1 alias edit sheet, living in the Library (§7.4): pin, default
 *  portion, delete — for one phrase → food mapping. */
export default function LibraryAliasSheet({
  alias,
  onSaved,
  onDeleted,
  onClose,
}: Props) {
  const sheetRef = useSheetFocus<HTMLDivElement>();
  const [pinned, setPinned] = useState(alias.pinned);
  const [servingId, setServingId] = useState(alias.default_serving_id ?? "");
  const [qtyText, setQtyText] = useState(
    alias.default_qty != null ? fmtQty(alias.default_qty) : "1",
  );
  const [servings, setServings] = useState<StagedServing[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  // The food's serving list comes from the detail route (foods.servings).
  useEffect(() => {
    let cancelled = false;
    getFood(alias.food_id)
      .then((f) => {
        if (!cancelled) setServings(f.servings ?? []);
      })
      .catch(() => {
        if (!cancelled) setServings([]);
      });
    return () => {
      cancelled = true;
    };
  }, [alias.food_id]);

  const qty = Number.parseFloat(qtyText);
  const qtyValid = Number.isFinite(qty) && qty > 0;
  const hasDefault = servingId !== "";

  const step = (d: number) => {
    const base = qtyValid ? qty : 1;
    setQtyText(fmtQty(Math.max(0.25, base + d)));
  };

  const save = async () => {
    if (hasDefault && !qtyValid) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await patchAlias(alias.id, {
        pinned,
        default_serving_id: hasDefault ? servingId : null,
        default_qty: hasDefault ? Math.round(qty * 100) / 100 : null,
      });
      onSaved({ ...alias, ...updated });
    } catch {
      setError("Couldn't save — try again.");
      setBusy(false);
    }
  };

  const remove = async () => {
    setBusy(true);
    setError(null);
    try {
      await deleteAlias(alias.id);
      onDeleted(alias.id);
    } catch {
      setError("Couldn't delete — try again.");
      setBusy(false);
    }
  };

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div
        ref={sheetRef}
        className="subsheet"
        role="dialog"
        aria-modal="true"
        aria-label="Edit phrase"
        tabIndex={-1}
      >
        <SheetHeader title={`“${alias.alias_norm}”`} onClose={onClose} />
        <p className="subsheet-hint">
          → {alias.food_name}
          {alias.brand ? ` — ${alias.brand}` : ""} · used{" "}
          <span className="num">{alias.use_count}×</span>
        </p>

        <div>
          <div className="edit-label">Pin</div>
          <div className="chips">
            <button
              type="button"
              className={`chip${pinned ? " active" : ""}`}
              aria-pressed={pinned}
              onClick={() => setPinned(!pinned)}
            >
              {pinned ? <span className="usual-dot" /> : null}
              {pinned ? "Pinned" : "Not pinned"}
            </button>
          </div>
          <p className="m3-note" style={{ marginTop: 6 }}>
            Pinned: this phrase always pre-selects this food and portion,
            whatever you logged most recently.
          </p>
        </div>

        <div>
          <div className="edit-label">Default portion</div>
          {hasDefault ? (
            <div className="stepper num" style={{ marginBottom: 8 }}>
              <button type="button" aria-label="Less" onClick={() => step(-0.5)}>
                −
              </button>
              <input
                value={qtyText}
                onChange={(e) => setQtyText(e.target.value)}
                inputMode="decimal"
                aria-label="Default quantity"
              />
              <button type="button" aria-label="More" onClick={() => step(0.5)}>
                +
              </button>
            </div>
          ) : null}
          <div className="serving-list">
            <button
              type="button"
              className={!hasDefault ? "active" : undefined}
              onClick={() => setServingId("")}
            >
              No default — use my recent portions
            </button>
            {(servings ?? []).map((s) => (
              <button
                key={s.serving_id}
                type="button"
                className={s.serving_id === servingId ? "active" : undefined}
                onClick={() => setServingId(s.serving_id)}
              >
                {s.description}
                {s.grams != null ? ` · ${Math.round(s.grams)} g` : ""}
              </button>
            ))}
            {servings == null ? (
              <p className="subsheet-hint">Loading servings…</p>
            ) : null}
          </div>
        </div>

        {error ? <p className="sheet-error">{error}</p> : null}

        <button
          type="button"
          className="commit-btn"
          disabled={busy || (hasDefault && !qtyValid)}
          onClick={save}
        >
          Save
        </button>

        <div className="m3-actions">
          {confirm ? (
            <>
              <span className="m3-note">Forget this phrase → food mapping?</span>
              <button
                type="button"
                className="m3-link-btn danger"
                disabled={busy}
                onClick={remove}
              >
                Yes, delete
              </button>
            </>
          ) : (
            <button
              type="button"
              className="m3-link-btn danger"
              disabled={busy}
              onClick={() => setConfirm(true)}
            >
              Delete mapping
            </button>
          )}
        </div>
      </div>
    </>
  );
}
