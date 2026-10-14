"use client";

import type { Meal, StagedItem } from "@/lib/api";
import { nutrientParts, portionText, withUnit } from "@/lib/format";
import { foodName, MEAL_LABEL } from "@/lib/labels";
import { Close } from "./icons";
import { readEstimate } from "./PhotoApi";
import "@/app/photo.css";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });

const MEALS: Meal[] = ["breakfast", "lunch", "dinner", "snack"];
const CORE_KEYS = new Set(["kcal", "protein_g", "carb_g", "fat_g"]);

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
  const name = foodName(item.food?.name ?? item.phrase);
  // the phrase under the name only when it says something the name doesn't
  // (foodName only re-cases, so a phrase that is the name in other case is
  // the same words)
  const said = item.phrase.toLowerCase() !== name.toLowerCase() ? item.phrase : null;
  const macros = item.macros;
  // §3.4a: photo-lane rows carry what vision guessed. "portion" = database
  // food, photo-estimated amount; "unresolved" = no database match at all —
  // what the photo saw, with no numbers. Both render distinct (the web-sourced
  // treatment is the reference). Since 2026-09-09 "portion" is a label rather
  // than a gate — the commit is the confirmation — while "unresolved" still
  // cannot commit at all (Search picks a food, or remove it).
  const est = readEstimate(item);
  const estimated = est.estimated !== "none";

  // the grams eaten, when the serving's weight is known — not the
  // per-serving "(148 g)" the description carries
  const servingGrams = item.servings?.find((s) => s.serving_id === item.serving?.id)?.grams;
  const portion =
    item.serving != null
      ? portionText(
          item.qty,
          item.serving.description,
          servingGrams != null && item.qty != null ? servingGrams * item.qty : null,
        )
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
                className="name-btn"
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
                would dangle, so the pill's own border does the separating,
                and with nothing after the brand there is nothing to separate */}
            {item.food?.brand
              ? `${item.food.brand}${portion ? " " : searching || notFound || said ? " · " : ""}`
              : ""}
            {portion ? (
              <button
                type="button"
                className={`portion-btn${verify ? " verify" : ""}`}
                aria-label={`Change portion for ${name}${verify ? ", check this amount" : ""}`}
                onClick={(e) => {
                  e.stopPropagation();
                  onPortion();
                }}
              >
                {/* the amber pill is the flag (§7.1: one signal); the label
                    says it for a screen reader. The '~' of an estimate sits
                    inside the pill, beside the amount it qualifies. */}
                {estimated ? "~" : ""}
                {portion}
              </button>
            ) : searching ? (
              "Looking it up…"
            ) : notFound ? (
              <span className="manual-hint">Not found — tap to search</span>
            ) : (
              said
            )}
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
          <Close />
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
            : est.note || "Portion estimated"}
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
                {foodName(opt.label)}
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
              {/* the answer sent stays the lowercase value */}
              {s === "cooked" ? "Cooked" : "Raw"}
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
              {foodName(alt.label)}
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
              Search instead
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
          {/* per-item meal chip: overrides the header for mixed logs (§7.1);
              a 4-option picker, never blind-cycle — the header's segmented
              row, so the four never wrap 3 + 1 on a narrow phone */}
          <div className="detail-meal">
            <span className="k">Meal</span>
            <div className="meal-picker m3-seg">
              {MEALS.map((m) => (
                <button
                  key={m}
                  type="button"
                  className={item.meal === m ? "active" : undefined}
                  onClick={() => onMeal(m)}
                >
                  {MEAL_LABEL[m]}
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
