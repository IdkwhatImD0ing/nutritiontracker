"use client";

import type { StagedItem } from "@/lib/api";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const CORE_KEYS = new Set(["kcal", "protein_g", "carb_g", "fat_g"]);
const UNITS = new Set(["g", "mg", "mcg", "ug", "iu", "kcal"]);

function humanizeKey(key: string): string {
  const parts = key.split("_");
  const last = parts[parts.length - 1]?.toLowerCase();
  const unit = last && UNITS.has(last) ? parts.pop() : null;
  const name = parts
    .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
    .join(" ");
  return unit ? `${name} (${unit})` : name;
}

/** A tapped question option or alternates chip. Id-bearing picks resolve
 * locally (§3.4/§7.1); an id-less label is a free-text answer. */
export type PickOption = {
  label: string;
  source?: string | null;
  source_id?: string | null;
  food_id?: string | null;
};

type Props = {
  item: StagedItem;
  expanded: boolean;
  busy: boolean;
  onToggle: () => void;
  onRemove: () => void;
  onPick: (opt: PickOption) => void;
};

export default function StagedRow({
  item,
  expanded,
  busy,
  onToggle,
  onRemove,
  onPick,
}: Props) {
  const searching = busy || item.status === "searching";
  const notFound = item.status === "not_found";
  const name = item.food?.name ?? item.phrase;
  const macros = item.macros;

  const portion =
    item.serving != null
      ? `${item.qty != null && item.qty !== 1 ? `${fmt1.format(item.qty)} × ` : ""}${item.serving.description}`
      : null;
  const verify = item.confidence === "low" || item.confidence === "medium";

  const microEntries = macros
    ? Object.entries(macros).filter(
        ([k, v]) => !CORE_KEYS.has(k) && typeof v === "number",
      )
    : [];

  return (
    <div
      className={`row${searching ? " searching" : ""}${notFound ? " not-found" : ""}`}
    >
      <div
        className="row-main"
        onClick={onToggle}
        role="button"
        aria-expanded={expanded}
      >
        <div className="row-info">
          <div className="row-name">
            {searching ? <span className="spinner" /> : null}
            {name}
            {searching ? (
              <span className="badge searching">searching</span>
            ) : null}
            {notFound ? <span className="badge not-found">not found</span> : null}
            {item.status === "needs_input" ? (
              <span className="badge needs-input">needs input</span>
            ) : null}
          </div>
          <div className="row-sub">
            {item.food?.brand ? `${item.food.brand} · ` : ""}
            {portion ? (
              <span className={verify ? "verify" : undefined}>
                {portion}
                {verify ? " · verify" : ""}
              </span>
            ) : searching ? (
              "resolving…"
            ) : notFound ? (
              "couldn't resolve — remove or retry later"
            ) : (
              item.phrase
            )}
          </div>
        </div>
        <div className="row-kcal num">
          <span className="n">
            {macros != null ? fmt.format(macros.kcal) : "—"}
          </span>
          <span className="u">kcal</span>
        </div>
        <button
          type="button"
          className="row-remove"
          aria-label={`Remove ${name}`}
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
        >
          ✕
        </button>
      </div>

      {item.status === "needs_input" && item.question ? (
        <>
          <p className="question-text">{item.question.text}</p>
          <div className="chips">
            {item.question.options.map((opt) => (
              <button
                key={opt.label}
                type="button"
                className="chip"
                disabled={busy}
                onClick={() => onPick(opt)}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </>
      ) : null}

      {item.status === "resolved" && item.alternates?.length ? (
        <div className="chips">
          {item.alternates.map((alt) => (
            <button
              key={alt.label}
              type="button"
              className="chip"
              disabled={busy}
              onClick={() => onPick(alt)}
            >
              {alt.usual ? <span className="usual-dot" /> : null}
              {alt.label}
            </button>
          ))}
        </div>
      ) : null}

      {expanded && macros != null ? (
        <div className="row-detail">
          <div className="macro-grid num">
            <div className="macro-cell protein">
              <div className="v">{fmt1.format(macros.protein_g)}g</div>
              <div className="k">Protein</div>
            </div>
            <div className="macro-cell">
              <div className="v">{fmt1.format(macros.carb_g)}g</div>
              <div className="k">Carbs</div>
            </div>
            <div className="macro-cell">
              <div className="v">{fmt1.format(macros.fat_g)}g</div>
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
          {portion ? (
            <div className="detail-serving">
              Portion: {portion}
              {item.qty != null ? ` (qty ${fmt1.format(item.qty)})` : ""}
              {item.tier != null ? ` · tier ${item.tier}` : ""}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
