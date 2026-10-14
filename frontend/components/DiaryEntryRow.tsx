"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import type { StagedServing } from "@/lib/api";
import type { DiaryEntry } from "./DiaryApi";
import { getFood } from "./LibraryApi";
import { qtyInput, rescaleQty, stepQty } from "./portion";
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
   *  the request); `serving_id` is sent only when the serving itself moved.
   *  Resolves true when the PATCH landed — the draft is kept on a refusal so
   *  the server's reason ("pick another serving") still has a draft to act on */
  onSave: (patch: { qty: number; serving_id?: string }) => Promise<boolean>;
  onDelete: () => void;
};

/** One committed diary row (§7.1 row display rule applied to the diary):
 *  the headline number is calories; tapping expands to protein/carbs/fat,
 *  then every micro the snapshot carries, and a "Food details" row (§7.4's
 *  drill-in); web-sourced rows show the citation as a visible, clickable
 *  link (§3.4a); photo-portion rows carry an "estimate" badge (M4); the
 *  portion — qty, and which serving it counts,
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

  // A collapsed row drops the draft: an abandoned qty or serving — or a
  // delete confirm — must not sit there armed behind a closed row. The
  // fetched list survives — it is this food's, not this draft's.
  useEffect(() => {
    if (expanded) return;
    setQtyText(null);
    setServingId(null);
    setPicking(false);
    setConfirmDelete(false);
  }, [expanded]);

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

  const draft = qtyText ?? qtyInput(entry.qty);
  const draftNum = Number(draft);
  const draftValid = Number.isFinite(draftNum) && draftNum > 0;
  // Only list servings when there is something to pick between; an empty list
  // is its own case, not "only the one".
  const choices = servings && servings.length > 1 ? servings : null;
  const chosenId = servingId ?? entry.serving.id;
  const chosenDesc =
    choices?.find((s) => s.serving_id === chosenId)?.description ??
    entry.serving.description;
  // A serving's weight: off the fetched list, or, for the entry's own
  // serving, off the entry (its grams are the serving's × qty).
  const gramsOf = (id: string) =>
    servings?.find((s) => s.serving_id === id)?.grams ??
    (id === entry.serving.id && entry.grams != null && entry.qty > 0
      ? entry.grams / entry.qty
      : null);
  const chosenGrams = gramsOf(chosenId);
  // The §4 snapshot invariant: the server refuses a serving whose macros are
  // partial. The same macros it checks are on this payload, so an unpickable
  // serving is shown disabled rather than round-tripped into a 422.
  const committable = (sv: StagedServing) =>
    [...CORE_KEYS].every((k) => typeof sv.macros?.[k] === "number");
  const servingMoved = chosenId !== entry.serving.id;
  const dirty =
    draftValid && (Math.abs(draftNum - entry.qty) > 1e-9 || servingMoved);

  const step = (dir: 1 | -1) =>
    setQtyText(qtyInput(stepQty(draftNum, dir, chosenGrams, 1)));

  // Another serving counts the same food: the draft is converted, not kept
  // ("0.5 × 1 cup" picked as "g" is 74 g, not 0.5 g). The draft, not the
  // entry's qty, so a number already typed carries over.
  const pickServing = (sv: StagedServing) => {
    if (sv.serving_id === chosenId) return;
    const next = draftValid
      ? rescaleQty(draftNum, chosenGrams, gramsOf(sv.serving_id))
      : null;
    if (next != null) setQtyText(qtyInput(next));
    setServingId(sv.serving_id);
  };

  return (
    <div className="row diary-row" data-entry-id={entry.id}>
      {/* One target (owner, 2026-10-13): a tap anywhere on the row expands
          it, and the chevron says so. The name used to be a link into food
          detail inside this toggle, and a long name covered most of the
          row, so a tap meant to expand drilled in instead; the drill-in is
          now the expanded panel's "Food details" row. The keys give the
          keyboard the same way in, since the name link no longer does. */}
      <div
        className="row-main"
        onClick={onToggle}
        onKeyDown={(e) => {
          if (e.key !== "Enter" && e.key !== " ") return;
          e.preventDefault(); // Space would scroll the page
          onToggle();
        }}
        role="button"
        tabIndex={0}
        aria-expanded={expanded}
      >
        <div className="row-info">
          <div className="row-name">
            <span className="food-name">{entry.food.name}</span>
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
        <span className="chev" aria-hidden="true" />
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

          {/* §7.4: food detail is a drill-in from any diary row — from here,
              one row down, so the row's own tap only ever expands it.
              prefetch off: /library/food/[id] is a dynamic auth-gated route,
              so a prefetch costs a middleware invocation for a page the user
              only reaches by deliberately tapping this. */}
          <Link
            className="diary-food-link"
            href={`/library/food/${encodeURIComponent(entry.food.id)}`}
            prefetch={false}
          >
            Food details
            <span className="chev" aria-hidden="true" />
          </Link>

          <div className="diary-edit">
            {/* a disclosure, not a one-way door: the same button closes the
                list again, which is the only way out on a food that turns out
                to have nothing to pick */}
            <button
              type="button"
              className="m3-link-btn diary-change-serving"
              aria-expanded={picking}
              onClick={() => setPicking((p) => !p)}
            >
              {picking ? "Hide servings" : "Change serving"}
            </button>
            {picking ? (
              choices ? (
                <div className="serving-list diary-servings">
                  {choices.map((sv) => {
                    const ok = committable(sv);
                    return (
                      <button
                        key={sv.serving_id}
                        type="button"
                        className={
                          sv.serving_id === chosenId ? "active" : undefined
                        }
                        disabled={busy || !ok}
                        title={
                          ok
                            ? undefined
                            : "This serving is missing macros, so it can't be logged."
                        }
                        onClick={() => pickServing(sv)}
                      >
                        {sv.description}
                        {ok ? "" : " · no macros"}
                      </button>
                    );
                  })}
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
                <p className="diary-note">
                  {!servings
                    ? "Loading servings…"
                    : servings.length === 1
                      ? "This food has only the one serving."
                      : "This food has no serving data."}
                </p>
              )
            ) : null}
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
            {/* While a delete is being confirmed the only choices are delete
                or keep: Save steps out (its busy spinner moves to the
                delete), and the two share the row equally, one line each
                even at 320 (diary.css). */}
            <div
              className={`diary-actions${confirmDelete ? " confirming" : ""}`}
            >
              {confirmDelete ? null : (
                <button
                  type="button"
                  className="diary-save"
                  disabled={busy || !dirty}
                  onClick={() => {
                    void onSave({
                      qty: draftNum,
                      ...(servingMoved ? { serving_id: chosenId } : {}),
                    }).then((saved) => {
                      // Only a landed PATCH hands the row back to the entry.
                      // Clearing eagerly flashed the old portion back for the
                      // length of the round trip, and threw the user's choice
                      // away exactly when a refusal asked them to change it.
                      if (!saved) return;
                      setQtyText(null);
                      setServingId(null);
                      setPicking(false);
                    });
                  }}
                >
                  {busy ? <span className="spinner" /> : null}
                  Save portion
                </button>
              )}
              {confirmDelete ? (
                <>
                  <button
                    type="button"
                    className="diary-delete confirm"
                    disabled={busy}
                    onClick={onDelete}
                  >
                    {busy ? <span className="spinner" /> : null}
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
