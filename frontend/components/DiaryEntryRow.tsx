"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import type { StagedServing } from "@/lib/api";
import { nutrientParts, portionText, statesGrams, withUnit } from "@/lib/format";
import { foodName } from "@/lib/labels";
import { sourceName } from "@/lib/sources";
import type { DiaryEntry } from "./DiaryApi";
import { getFood } from "./LibraryApi";
import PortionSheet, { type PortionChoice } from "./PortionSheet";
import "@/app/photo.css"; // .badge.estimate

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const CORE_KEYS = new Set(["kcal", "protein_g", "carb_g", "fat_g"]);

type Props = {
  entry: DiaryEntry;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  /** portion edit → PATCH /api/entries/:id {qty, serving_id?} (parent owns
   *  the request); `serving_id` is sent only when the serving itself moved.
   *  Resolves null when the PATCH landed, else the sentence to show — the
   *  portion sheet stays open on a refusal, so the server's reason ("pick
   *  another serving") sits beside the choice it asks to change */
  onSave: (patch: {
    qty: number;
    serving_id?: string;
  }) => Promise<string | null>;
  onDelete: () => void;
};

/** One committed diary row (§7.1 row display rule applied to the diary):
 *  the headline number is calories; tapping expands to protein/carbs/fat,
 *  then every micro the snapshot carries; web-sourced rows show the
 *  citation as a visible, clickable link (§3.4a); photo-portion rows carry
 *  an "estimate" badge (M4); the portion pill opens the same portion sheet a
 *  staged row does (§7.1) and the row is deletable (§5 `/api/entries/:id`).
 *  The serving list costs a GET /api/foods/:id whose FoodDetail carries the
 *  whole provider payload, so it is fetched on the pill's tap and not on the
 *  expand: expanding a row is how the micros are read, and most rows are
 *  read rather than corrected. */
export default function DiaryEntryRow({
  entry,
  expanded,
  busy,
  onToggle,
  onSave,
  onDelete,
}: Props) {
  const [portionOpen, setPortionOpen] = useState(false);
  const [servings, setServings] = useState<StagedServing[] | null>(null);
  const [servingsFailed, setServingsFailed] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const deleteBtn = useRef<HTMLButtonElement>(null);

  // The food's servings, fetched when the portion sheet first opens and never
  // again for this row (a foods row's serving list does not change under us).
  const foodId = entry.food.id;
  useEffect(() => {
    if (!portionOpen || servings || servingsFailed) return;
    let cancelled = false;
    getFood(foodId)
      .then((f) => {
        if (!cancelled) setServings(f.servings);
      })
      .catch(() => {
        if (!cancelled) setServingsFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [portionOpen, foodId, servings, servingsFailed]);

  const name = foodName(entry.food.name);
  const m = entry.macros;
  // BACKLOG 28: the macros jsonb carries a `base_*` sidecar on qty-edited
  // rows — diary.py passes it through as extra float keys; never render it.
  const microEntries = Object.entries(m).filter(
    ([k, v]) =>
      !CORE_KEYS.has(k) && !k.startsWith("base_") && typeof v === "number",
  );
  // "0.5 × 1 cup · 74 g": the grams eaten, not the per-serving "(148 g)"
  const portion = portionText(entry.qty, entry.serving.description, entry.grams);

  // The sheet's list: the food's servings once fetched, with this entry's
  // own serving always in it, so nothing but a tap moves the serving. A
  // failed fetch leaves the entry's serving alone — the amount still changes.
  const own: PortionChoice = {
    id: entry.serving.id,
    description: entry.serving.description,
  };
  const choices: PortionChoice[] | null = servingsFailed
    ? [own]
    : servings && [
        ...(servings.some((sv) => sv.serving_id === own.id) ? [] : [own]),
        ...servings.map((sv) => ({
          id: sv.serving_id,
          description: sv.description,
          macros: sv.macros,
        })),
      ];
  // The §4 snapshot invariant: the server refuses a serving whose macros are
  // partial. The same macros it checks are on this payload, so an unpickable
  // serving is shown disabled rather than round-tripped into a 422. The
  // entry's own serving is the one already logged, never disabled.
  const unpickable = (sv: PortionChoice) =>
    sv.id !== own.id &&
    ![...CORE_KEYS].every((k) => typeof sv.macros?.[k] === "number");

  const openPortion = () => {
    setSaveError(null);
    setServingsFailed(false); // a failed fetch retries on the next open
    setPortionOpen(true);
  };

  const applyPortion = (serving: PortionChoice, qty: number) => {
    const moved = serving.id !== own.id;
    // nothing changed (the sheet shows qty to 2 places): Done just closes
    if (!moved && Math.abs(qty - Math.round(entry.qty * 100) / 100) < 1e-9) {
      setPortionOpen(false);
      return;
    }
    setSaveError(null);
    void onSave({ qty, ...(moved ? { serving_id: serving.id } : {}) }).then(
      (refusal) => {
        // Only a landed PATCH closes the sheet; a refusal keeps the choice
        // on screen next to the server's reason to change it.
        if (refusal) setSaveError(refusal);
        else setPortionOpen(false);
      },
    );
  };

  return (
    <div className={`row diary-row${entry.web_source ? " web" : ""}`}>
      <div
        className="row-main"
        onClick={onToggle}
        role="button"
        aria-expanded={expanded}
      >
        <div className="row-info">
          <div className="row-name">
            {/* §7.4: food detail is a drill-in from any diary row. The row's
                own tap still toggles the expander. prefetch off: this renders
                once per diary row, and /library/food/[id] is a dynamic
                auth-gated route, so the default viewport prefetch costs a
                middleware invocation per row on screen for a page the user
                only reaches by deliberately tapping the name. */}
            <Link
              className="name-btn"
              href={`/library/food/${encodeURIComponent(entry.food.id)}`}
              prefetch={false}
              onClick={(e) => e.stopPropagation()}
            >
              {name}
            </Link>
            {entry.web_source ? <span className="badge web">web</span> : null}
            {/* M4: the portion was read off a photo (and confirmed before
                commit) — flagged the way the staged row was. */}
            {entry.estimated === "portion" ? (
              <span className="badge estimate">estimate</span>
            ) : null}
          </div>
          <div className="row-sub">
            {entry.food.brand ? `${entry.food.brand} · ` : ""}
            {portion}
          </div>
        </div>
        <div className="row-kcal num">
          <span className="n">{fmt.format(m.kcal)}</span>
          <span className="u">kcal</span>
        </div>
      </div>

      {/* §3.4a: visible, clickable citation — collapsed or expanded. */}
      {entry.web_source ? (
        <p className="web-cite">
          Source:{" "}
          <a
            href={entry.web_source.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            {entry.web_source.title}
          </a>
        </p>
      ) : null}

      {expanded ? (
        <div className="row-detail">
          <div className="macro-grid num">
            <div className="macro-cell protein">
              <div className="v">{fmt1.format(m.protein_g)}g</div>
              <div className="k">Protein</div>
            </div>
            <div className="macro-cell">
              <div className="v">{fmt1.format(m.carb_g)}g</div>
              <div className="k">Carbs</div>
            </div>
            <div className="macro-cell">
              <div className="v">{fmt1.format(m.fat_g)}g</div>
              <div className="k">Fat</div>
            </div>
          </div>
          {microEntries.length ? (
            <div className="micro-list num">
              {microEntries.map(([k, v]) => {
                const { name: label, unit } = nutrientParts(k);
                return (
                  <div className="micro-line" key={k}>
                    <span className="k">{label}</span>
                    <span>{withUnit(v as number, unit)}</span>
                  </div>
                );
              })}
            </div>
          ) : null}

          <div className="detail-serving">
            Portion:{" "}
            <button
              type="button"
              className="portion-btn"
              aria-label={`Change portion for ${name}`}
              disabled={busy}
              onClick={openPortion}
            >
              {portion}
            </button>
            {/* portionText names the grams for any other qty; one serving
                whose description doesn't state them still gets them here */}
            {entry.qty === 1 && entry.grams != null && !statesGrams(entry.serving.description)
              ? ` · ${withUnit(fmt.format(entry.grams), "g")}`
              : ""}
            {/* a web row's badge and citation are on its name line already */}
            {entry.source && !entry.web_source ? (
              <span className={`badge${entry.source === "web" ? " web" : ""}`}>
                {sourceName(entry.source)}
              </span>
            ) : null}
          </div>

          <div className="diary-edit">
            <div className="diary-actions">
              {confirmDelete ? (
                <>
                  {/* the app's one wording for an irreversible delete
                      (ApiKeysCard's revoke): a red yes and a focused Cancel */}
                  <button
                    type="button"
                    className="diary-delete confirm"
                    disabled={busy}
                    onClick={onDelete}
                  >
                    Yes, delete
                  </button>
                  <button
                    type="button"
                    className="diary-cancel"
                    disabled={busy}
                    onClick={() => {
                      setConfirmDelete(false);
                      requestAnimationFrame(() => deleteBtn.current?.focus());
                    }}
                    autoFocus
                  >
                    Cancel
                  </button>
                </>
              ) : (
                <button
                  ref={deleteBtn}
                  type="button"
                  className="diary-delete"
                  disabled={busy}
                  onClick={() => setConfirmDelete(true)}
                >
                  Delete
                </button>
              )}
            </div>
          </div>
        </div>
      ) : null}

      {portionOpen ? (
        <PortionSheet
          title={name}
          servings={choices}
          initialServingId={own.id}
          initialQty={entry.qty}
          initialKcal={m.kcal}
          isDisabled={unpickable}
          error={
            saveError ??
            (servingsFailed
              ? "Couldn’t load this food’s other servings — reopen to try again."
              : null)
          }
          busy={busy}
          onApply={applyPortion}
          onClose={() => setPortionOpen(false)}
        />
      ) : null}
    </div>
  );
}
