"use client";

import type { Meal, StagedItem } from "@/lib/api";
import { readEstimate } from "./PhotoApi";
import "@/app/photo.css";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const MEALS: Meal[] = ["breakfast", "lunch", "dinner", "snack"];
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
  /** tap the portion → §7.1 portion sheet */
  onPortion: () => void;
  /** not_found rows + the §3.3 rule-3 "search instead" chip → BACKLOG 3
   *  manual-search sheet */
  onManualSearch: () => void;
  /** per-item meal override (§7.1 — the header chip propagates; this wins) */
  onMeal: (m: Meal) => void;
  /** §7.1 food bullet (BACKLOG 23): tap the name → edit sheet (aliases,
   *  re-resolving text field, manual search) */
  onEditFood: () => void;
};

export default function StagedRow({
  item,
  expanded,
  busy,
  onToggle,
  onRemove,
  onPick,
  onPortion,
  onManualSearch,
  onMeal,
  onEditFood,
}: Props) {
  const searching = busy || item.status === "searching";
  const notFound = item.status === "not_found";
  const editable = !searching && !notFound; // §7.1: resolved/needs_input rows
  const name = item.food?.name ?? item.phrase;
  const macros = item.macros;
  // §3.4a: photo-lane rows carry what vision guessed. "portion" = database
  // food, photo-estimated amount; "unresolved" = no database match at all —
  // what the photo saw, with no numbers. Both render distinct (the web-sourced
  // treatment is the reference). Since 2026-09-09 "portion" is a label rather
  // than a gate — the commit is the confirmation — while "unresolved" still
  // cannot commit at all (Search picks a food, or remove it).
  const est = readEstimate(item);
  const estimated = est.estimated !== "none";

  const portion =
    item.serving != null
      ? `${item.qty != null && item.qty !== 1 ? `${fmt1.format(item.qty)} × ` : ""}${item.serving.description}`
      : null;
  const verify =
    item.confidence === "low" ||
    item.confidence === "medium" ||
    est.estimated === "portion";
  // An unresolved row has no database numbers — offer the same escape hatch a
  // not_found row gets (§3.4 "search instead").
  const searchInstead = item.search_instead || est.estimated === "unresolved";

  const microEntries = macros
    ? Object.entries(macros).filter(
        ([k, v]) => !CORE_KEYS.has(k) && typeof v === "number",
      )
    : [];

  return (
    <div
      className={`row${searching ? " searching" : ""}${notFound ? " not-found" : ""}${estimated ? " estimated" : ""}`}
    >
      <div
        className="row-main"
        onClick={notFound ? onManualSearch : onToggle}
        role="button"
        aria-expanded={notFound ? undefined : expanded}
      >
        <div className="row-info">
          <div className="row-name">
            {searching ? <span className="spinner" /> : null}
            {editable ? (
              <button
                type="button"
                className="name-btn food-name"
                aria-label={`Edit food for ${name}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onEditFood();
                }}
              >
                {name}
              </button>
            ) : (
              name
            )}
            {/* no status badges: the spinner and the sub-line say searching
                and not found, and the question under the row says it needs
                an answer */}
            {item.web_source ? (
              <span className="badge web">web</span>
            ) : null}
            {estimated ? (
              <span className="badge estimate">estimate</span>
            ) : null}
          </div>
          <div className="row-sub">
            {/* the middot separates two text runs; before the portion pill it
                would dangle, so the pill's own border does the separating */}
            {item.food?.brand ? `${item.food.brand}${portion ? " " : " · "}` : ""}
            {portion ? (
              <button
                type="button"
                className={`portion-btn${verify ? " verify" : ""}`}
                aria-label={`Change portion for ${name}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onPortion();
                }}
              >
                {portion}
                {verify ? " · verify" : ""}
              </button>
            ) : searching ? (
              "Looking it up…"
            ) : notFound ? (
              <span className="manual-hint">Not found — tap to search</span>
            ) : item.phrase !== name ? (
              item.phrase
            ) : null}
          </div>
        </div>
        <div className="row-kcal num">
          <span className="n">
            {macros != null
              ? `${estimated ? "~" : ""}${fmt.format(macros.kcal)}`
              : "—"}
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

      {/* §3.4a: web-transcribed foods render visually distinct with the
          citation as a visible, clickable link — an OpenAI usage-terms
          requirement for web-search content, shown collapsed or expanded. */}
      {item.web_source ? (
        <p className="web-cite">
          Source:{" "}
          <a
            href={item.web_source.url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={(e) => e.stopPropagation()}
          >
            {item.web_source.title}
          </a>
        </p>
      ) : null}

      {/* §3.4a: say what is a guess, in the row, whatever the expansion
          state — a portion read off the photo or sized by the agent (§6.4;
          the note says which and how), or a whole food no database matched
          (unresolved: what the photo saw, no numbers). */}
      {estimated ? (
        <p className="estimate-note">
          {est.estimated === "unresolved"
            ? `${est.note ? `${est.note} · ` : ""}No database match, so no numbers — search to pick a food`
            : `Portion estimated${est.note ? ` · ${est.note}` : ""}`}
        </p>
      ) : null}

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

      {/* §7.1 cooked/raw toggle: one tap; the answer flows through
          /api/resolve/answer and persists to the alias — asked at most once
          per phrase. */}
      {item.status === "resolved" && item.state_question ? (
        <div className="chips">
          <span className="state-q">Cooked or raw?</span>
          {(["cooked", "raw"] as const).map((s) => (
            <button
              key={s}
              type="button"
              className="chip"
              disabled={busy}
              onClick={() => onPick({ label: s })}
            >
              {s}
            </button>
          ))}
        </div>
      ) : null}

      {item.status === "resolved" &&
      (item.alternates?.length || searchInstead) ? (
        <div className="chips">
          {(item.alternates ?? []).map((alt) => (
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
          {/* §3.3 rule 3 (BACKLOG 32): a weak-trigram-only match resolves the
              top hit but offers the escape hatch inline — one tap to the
              manual-search sheet instead of committing a guess. */}
          {searchInstead ? (
            <button
              type="button"
              className="chip search-instead"
              disabled={busy}
              onClick={onManualSearch}
            >
              search instead
            </button>
          ) : null}
        </div>
      ) : null}

      {expanded && !notFound ? (
        <div className="row-detail">
          {macros != null ? (
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
          ) : null}
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
          {/* per-item meal chip: overrides the header for mixed logs (§7.1);
              a 4-option picker, never blind-cycle */}
          <div className="detail-meal">
            <span className="k">Meal</span>
            <div className="chips">
              {MEALS.map((m) => (
                <button
                  key={m}
                  type="button"
                  className={`chip${item.meal === m ? " active" : ""}`}
                  onClick={() => onMeal(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          </div>
          {portion ? (
            <div className="detail-serving">
              Portion: {portion}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
