"use client";

import Link from "next/link";
import { useState } from "react";
import type { DiaryEntry } from "./DiaryApi";
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
  /** qty edit → PATCH /api/entries/:id {qty} (parent owns the request) */
  onQty: (qty: number) => void;
  onDelete: () => void;
};

/** One committed diary row (§7.1 row display rule applied to the diary):
 *  the headline number is calories; tapping expands to protein/carbs/fat,
 *  then every micro the snapshot carries; web-sourced rows show the
 *  citation as a visible, clickable link (§3.4a); photo-portion rows carry
 *  an "estimate" badge (M4); qty is editable and the row is deletable (§5
 *  `/api/entries/:id`). */
export default function DiaryEntryRow({
  entry,
  expanded,
  busy,
  onToggle,
  onQty,
  onDelete,
}: Props) {
  const [qtyText, setQtyText] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

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
  const dirty = draftValid && Math.abs(draftNum - entry.qty) > 1e-9;

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
              className="name-btn"
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
            <span className="k">Qty · {entry.serving.description}</span>
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
                  onQty(draftNum);
                  setQtyText(null);
                }}
              >
                {busy ? <span className="spinner" /> : null}
                Save qty
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
