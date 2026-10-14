"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { StagedServing } from "@/lib/api";
import { fmt0, withUnit } from "@/lib/format";
import { foodName } from "@/lib/labels";
import { Check } from "./icons";
import { deleteAlias, getFood, patchAlias, type AliasRow } from "./LibraryApi";
import SheetHeader from "./SheetHeader";

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
  const [pinned, setPinned] = useState(alias.pinned);
  const [servingId, setServingId] = useState(alias.default_serving_id ?? "");
  const [qtyText, setQtyText] = useState(
    alias.default_qty != null ? fmtQty(alias.default_qty) : "1",
  );
  const [servings, setServings] = useState<StagedServing[] | null>(null);
  // A failed read leaves `servings` null rather than [], which would read as
  // "this food has no servings"; `attempt` re-runs it.
  const [servingsFailed, setServingsFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  // which request is in flight: only Save shows a spinner for its own
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const deleteBtn = useRef<HTMLButtonElement>(null);
  const pinLabel = useId();

  // The food's serving list comes from the detail route (foods.servings).
  useEffect(() => {
    let cancelled = false;
    getFood(alias.food_id)
      .then((f) => {
        if (!cancelled) setServings(f.servings ?? []);
      })
      .catch(() => {
        if (!cancelled) setServingsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [alias.food_id, attempt]);

  const qty = Number.parseFloat(qtyText);
  const qtyValid = Number.isFinite(qty) && qty > 0;
  const hasDefault = servingId !== "";

  const step = (d: number) => {
    const base = qtyValid ? qty : 1;
    setQtyText(fmtQty(Math.max(0.25, base + d)));
  };

  const save = async () => {
    if (hasDefault && !qtyValid) return;
    setBusy("save");
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
      setBusy(null);
    }
  };

  const remove = async () => {
    setBusy("delete");
    setError(null);
    try {
      await deleteAlias(alias.id);
      onDeleted(alias.id);
    } catch {
      setError("Couldn't delete — try again.");
      setBusy(null);
    }
  };

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Edit phrase">
        <SheetHeader title={`“${alias.alias_norm}”`} onClose={onClose} />
        <p className="subsheet-hint">
          Logs as {foodName(alias.food_name)}
          {alias.brand ? ` (${alias.brand})` : ""} · used{" "}
          <span className="num">{fmt0.format(alias.use_count)}×</span>
        </p>

        <div>
          {/* the pin is a setting, so a switch: a chip named for its own
              state read as a status tag, not a control. The label names it
              and takes the tap too. */}
          <label className="m3-row">
            <span id={pinLabel}>Always use this food and portion</span>
            <button
              type="button"
              role="switch"
              aria-checked={pinned}
              aria-labelledby={pinLabel}
              className="switch"
              onClick={() => setPinned(!pinned)}
            />
          </label>
          <p className="m3-note" style={{ marginTop: 6 }}>
            When on, this phrase always pre-selects this food and portion,
            whatever you logged most recently.
          </p>
        </div>

        <div>
          <div className="edit-label">Default portion</div>
          {/* Nothing to pick until the list is in: a stored default can't be
              cleared against servings he never saw. Save still resends it. */}
          {servings == null ? (
            servingsFailed ? (
              <button
                type="button"
                className="m3-link-btn"
                onClick={() => {
                  setServingsFailed(false);
                  setAttempt((n) => n + 1);
                }}
              >
                Couldn’t load this food’s servings — retry
              </button>
            ) : (
              <p className="subsheet-hint">Loading servings…</p>
            )
          ) : (
            <>
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
                  <span>No default — use my recent portions</span>
                  {!hasDefault ? <Check /> : null}
                </button>
                {servings.map((s) => (
                  <button
                    key={s.serving_id}
                    type="button"
                    className={s.serving_id === servingId ? "active" : undefined}
                    onClick={() => setServingId(s.serving_id)}
                  >
                    <span>
                      {s.description}
                      {/* kcal, not grams: FDC descriptions already state theirs
                          ("1 container (170 g)"), so a grams suffix repeated them */}
                      {typeof s.macros?.kcal === "number"
                        ? ` · ${withUnit(fmt0.format(s.macros.kcal), "kcal")}`
                        : ""}
                    </span>
                    {s.serving_id === servingId ? <Check /> : null}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        {error ? <p className="sheet-error">{error}</p> : null}

        <button
          type="button"
          className="commit-btn"
          disabled={busy !== null || (hasDefault && !qtyValid)}
          aria-busy={busy === "save"}
          onClick={save}
        >
          {busy === "save" ? (
            <>
              <span className="spinner on-accent" />
              Saving…
            </>
          ) : (
            "Save"
          )}
        </button>

        {/* forgetting a phrase can't be undone: ApiKeysCard's inline
            confirm, Cancel first and focused, then the red yes */}
        <div className={`m3-actions${confirm ? " confirm" : ""}`}>
          {confirm ? (
            <>
              <span className="m3-note">
                Forget this phrase? Tally will stop matching it to this food.
              </span>
              <button
                type="button"
                className="m3-link-btn"
                disabled={busy !== null}
                onClick={() => {
                  setConfirm(false);
                  requestAnimationFrame(() => deleteBtn.current?.focus());
                }}
                autoFocus
              >
                Cancel
              </button>
              <button
                type="button"
                className="m3-link-btn danger"
                disabled={busy !== null}
                onClick={remove}
              >
                Yes, forget
              </button>
            </>
          ) : (
            <button
              ref={deleteBtn}
              type="button"
              className="m3-link-btn danger"
              disabled={busy !== null}
              onClick={() => setConfirm(true)}
            >
              Forget phrase
            </button>
          )}
        </div>
      </div>
    </>
  );
}
