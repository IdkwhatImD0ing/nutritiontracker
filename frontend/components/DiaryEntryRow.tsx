"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { StagedServing } from "@/lib/api";
import type { DiaryEntry } from "./DiaryApi";
import { getFood } from "./LibraryApi";
import "@/app/photo.css"; // .badge.estimate

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const fmt2 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

const CORE_KEYS = new Set(["kcal", "protein_g", "carb_g", "fat_g"]);
const UNITS = new Set(["g", "mg", "mcg", "ug", "iu", "kcal"]);

/** Same key→label rule as StagedRow: "vitamin_c_mg" → "Vitamin C (mg)". */
function humanizeKey(key: string): string {
  const parts = key.split("_");
  const last = parts[parts.length - 1]?.toLowerCase();
  const unit = last && UNITS.has(last) ? parts.pop() : null;
  const name = parts
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
  return unit ? `${name} (${unit})` : name;
}

type Props = {
  entry: DiaryEntry;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  /** portion edit → PATCH /api/entries/:id {qty, serving_id?} (parent owns
   *  the request); `serving_id` is sent only when the serving itself moved */
  onSave: (patch: { qty: number; serving_id?: string }) => void;
  onDelete: () => void;
};

/** One committed diary row (§7.1 row display rule applied to the diary):
 *  the headline number is calories; tapping expands to protein/carbs/fat,
 *  then every micro the snapshot carries; web-sourced rows show the
 *  citation as a visible, clickable link (§3.4a); photo-portion rows carry
 *  an "estimate" badge (M4); the portion — qty, and which serving it counts,
 *  behind "Change serving" — is editable and the row is deletable (§5
 *  `/api/entries/:id`). The serving list costs a GET /api/foods/:id whose
 *  FoodDetail carries the whole provider payload, so it is fetched on that
 *  tap and not on the expand: expanding a row is how the micros are read,
 *  and most rows are read rather than corrected. */
export default function DiaryEntryRow({
  entry,
  expanded,
  busy,
  onToggle,
  onSave,
  onDelete,
}: Props) {
  const [qtyText, setQtyText] = useState<string | null>(null);
  const [servingId, setServingId] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const [servings, setServings] = useState<StagedServing[] | null>(null);
  const [servingsFailed, setServingsFailed] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  // The food's servings, fetched on the first "Change serving" and never
  // again for this row (a foods row's serving list does not change under us).
  const foodId = entry.food.id;
  useEffect(() => {
    if (!picking || servings || servingsFailed) return;
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
  }, [picking, foodId, servings, servingsFailed]);

  const m = entry.macros;
  // BACKLOG 28: the macros jsonb carries a `base_*` sidecar on qty-edited
  // rows — diary.py passes it through as extra float keys; never render it.
  const microEntries = Object.entries(m).filter(
    ([k, v]) =>
      !CORE_KEYS.has(k) && !k.startsWith("base_") && typeof v === "number",
  );
  const portion = `${entry.qty !== 1 ? `${fmt2.format(entry.qty)} × ` : ""}${
    entry.serving.description
  }`;

  const draft = qtyText ?? fmt2.format(entry.qty);
  const draftNum = Number(draft);
  const draftValid = Number.isFinite(draftNum) && draftNum > 0;
  // Only list servings when there is something to pick between.
  const choices = servings && servings.length > 1 ? servings : null;
  const chosenId = servingId ?? entry.serving.id;
  const chosenDesc =
    choices?.find((s) => s.serving_id === chosenId)?.description ??
    entry.serving.description;
  const servingMoved = chosenId !== entry.serving.id;
  const dirty =
    draftValid && (Math.abs(draftNum - entry.qty) > 1e-9 || servingMoved);

  const step = (delta: number) => {
    const next = Math.max(0.25, Math.round((draftNum + delta) * 100) / 100);
    setQtyText(fmt2.format(next));
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
                own tap still toggles the expander. */}
            <Link
              className="name-btn food-name"
              href={`/library/food/${encodeURIComponent(entry.food.id)}`}
              onClick={(e) => e.stopPropagation()}
            >
              {entry.food.name}
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
              {microEntries.map(([k, v]) => (
                <div className="micro-line" key={k}>
                  <span className="k">{humanizeKey(k)}</span>
                  <span>{fmt1.format(v as number)}</span>
                </div>
              ))}
            </div>
          ) : null}

          <div className="detail-serving">
            Portion: {portion}
            {entry.grams != null ? ` · ${fmt.format(entry.grams)} g` : ""}
            {entry.source ? (
              <span className={`badge${entry.source === "web" ? " web" : ""}`}>
                {entry.source}
              </span>
            ) : null}
          </div>

          <div className="diary-edit">
            {picking ? (
              <>
                {choices ? (
                  <div className="serving-list diary-servings">
                    {choices.map((s) => (
                      <button
                        key={s.serving_id}
                        type="button"
                        className={
                          s.serving_id === chosenId ? "active" : undefined
                        }
                        disabled={busy}
                        onClick={() => setServingId(s.serving_id)}
                      >
                        {s.description}
                      </button>
                    ))}
                  </div>
                ) : servingsFailed ? (
                  // the retry re-arms the fetch: `picking` is still true, so
                  // clearing the flag is all the effect needs
                  <button
                    type="button"
                    className="m3-link-btn diary-change-serving"
                    onClick={() => setServingsFailed(false)}
                  >
                    Couldn’t load this food’s servings — retry
                  </button>
                ) : (
                  <span className="k">
                    {servings
                      ? "This food has only the one serving."
                      : "Loading servings…"}
                  </span>
                )}
              </>
            ) : (
              <button
                type="button"
                className="m3-link-btn diary-change-serving"
                onClick={() => setPicking(true)}
              >
                Change serving
              </button>
            )}
            <span className="k">Qty · {chosenDesc}</span>
            <div className="stepper diary-stepper">
              <button
                type="button"
                aria-label="Less"
                disabled={busy || !draftValid}
                onClick={() => step(-1)}
              >
                −
              </button>
              <input
                className="num"
                inputMode="decimal"
                value={draft}
                aria-label="Quantity"
                disabled={busy}
                onChange={(e) => setQtyText(e.target.value)}
              />
              <button
                type="button"
                aria-label="More"
                disabled={busy || !draftValid}
                onClick={() => step(1)}
              >
                +
              </button>
            </div>
            <div className="diary-actions">
              <button
                type="button"
                className="diary-save"
                disabled={busy || !dirty}
                onClick={() => {
                  onSave({
                    qty: draftNum,
                    ...(servingMoved ? { serving_id: chosenId } : {}),
                  });
                  // back to tracking the entry: the parent hands the patched
                  // one down, and a refused edit must not leave the row
                  // showing a portion the server rejected
                  setQtyText(null);
                  setServingId(null);
                }}
              >
                {busy ? <span className="spinner" /> : null}
                Save portion
              </button>
              {confirmDelete ? (
                <>
                  <button
                    type="button"
                    className="diary-delete confirm"
                    disabled={busy}
                    onClick={onDelete}
                  >
                    Really delete
                  </button>
                  <button
                    type="button"
                    className="diary-cancel"
                    disabled={busy}
                    onClick={() => setConfirmDelete(false)}
                  >
                    Keep
                  </button>
                </>
              ) : (
                <button
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
    </div>
  );
}
