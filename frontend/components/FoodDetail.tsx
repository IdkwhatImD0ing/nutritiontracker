"use client";

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { foodName } from "@/lib/labels";
import BackBar from "./BackBar";
import {
  getFood,
  type AliasRow,
  type FoodDetail as Food,
  type NutrientRow,
} from "./LibraryApi";
import LibraryAliasSheet from "./LibraryAliasSheet";

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const fmt1 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
/** the raw dump keeps the source's precision — 0.02 mg must not read "0" */
const fmt3 = new Intl.NumberFormat("en-US", { maximumFractionDigits: 3 });

const CORE = new Set(["kcal", "protein_g", "carb_g", "fat_g"]);

/** The nutrients the adapters know how to read (sources/fdc.py's id map).
 *  Listed in full so an absent one renders "not measured" — never zero. */
const NUTRIENTS: [string, string][] = [
  ["fiber_g", "Fiber"],
  ["sugar_g", "Sugar"],
  ["added_sugar_g", "Added sugar"],
  ["saturated_fat_g", "Saturated fat"],
  ["trans_fat_g", "Trans fat"],
  ["monounsaturated_fat_g", "Monounsaturated fat"],
  ["polyunsaturated_fat_g", "Polyunsaturated fat"],
  ["cholesterol_mg", "Cholesterol"],
  ["sodium_mg", "Sodium"],
  ["potassium_mg", "Potassium"],
  ["calcium_mg", "Calcium"],
  ["iron_mg", "Iron"],
  ["magnesium_mg", "Magnesium"],
  ["phosphorus_mg", "Phosphorus"],
  ["zinc_mg", "Zinc"],
  ["vitamin_c_mg", "Vitamin C"],
  ["vitamin_e_mg", "Vitamin E"],
  ["thiamin_mg", "Thiamin"],
  ["riboflavin_mg", "Riboflavin"],
  ["niacin_mg", "Niacin"],
  ["vitamin_b6_mg", "Vitamin B6"],
];
const KNOWN = new Set(NUTRIENTS.map(([k]) => k));
const UNITS = new Set(["g", "mg", "mcg", "ug", "iu"]);

function unitOf(key: string): string {
  const last = key.split("_").pop() ?? "";
  if (!UNITS.has(last)) return "";
  return last === "ug" ? "mcg" : last === "iu" ? "IU" : last;
}

function humanize(key: string): string {
  const parts = key.split("_");
  if (UNITS.has(parts[parts.length - 1] ?? "")) parts.pop();
  return parts.map((p) => p.charAt(0).toUpperCase() + p.slice(1)).join(" ");
}

function sourceBadge(source: string): { cls: string; label: string } {
  if (source === "fdc") return { cls: "fdc", label: "USDA FDC" };
  if (source === "off") return { cls: "off", label: "Open Food Facts" };
  if (source === "web") return { cls: "web", label: "web" };
  return { cls: "state", label: source };
}

/** The citation link (§3.4a for web foods; the record page for databases). */
function sourceLink(f: Food): { href: string; label: string } | null {
  if (f.source === "fdc") {
    return {
      href: `https://fdc.nal.usda.gov/food-details/${encodeURIComponent(f.source_id)}/nutrients`,
      label: "USDA FoodData Central",
    };
  }
  if (f.source === "off") {
    return {
      href: `https://world.openfoodfacts.org/product/${encodeURIComponent(f.source_id)}`,
      label: "Open Food Facts",
    };
  }
  if (f.source === "web" && f.source_url) {
    let label = f.provenance?.page_title ?? null;
    if (!label) {
      try {
        label = new URL(f.source_url).hostname;
      } catch {
        label = f.source_url;
      }
    }
    return { href: f.source_url, label };
  }
  return null;
}

/** The route's `nutrients` list grouped by its denominator ("per 100 g",
 *  "per serving (label)", …) with source order preserved inside a group. */
function byBasis(rows: NutrientRow[]): [string, NutrientRow[]][] {
  const groups = new Map<string, NutrientRow[]>();
  for (const n of rows) {
    const g = groups.get(n.basis);
    if (g) g.push(n);
    else groups.set(n.basis, [n]);
  }
  return [...groups.entries()];
}

function NutrientLine({ k, label, v }: { k: string; label: string; v: unknown }) {
  return (
    <div className="nutrient-line">
      <span className="k">{label}</span>
      {typeof v === "number" ? (
        <span>
          {fmt1.format(v)} {unitOf(k)}
        </span>
      ) : (
        <span className="nm">not measured</span>
      )}
    </div>
  );
}

/** §7.4 food detail: the §7.1 "everything the database returned" panel —
 *  macros, every nutrient the source carried, the serving definitions, the
 *  phrases pointing here (pin / default portion / delete), and the source
 *  badge with date + citation link. */
export default function FoodDetail({ id }: { id: string }) {
  const [food, setFood] = useState<Food | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [servingId, setServingId] = useState("");
  const [aliasSheet, setAliasSheet] = useState<AliasRow | null>(null);

  useEffect(() => {
    let cancelled = false;
    getFood(id)
      .then((f) => {
        if (cancelled) return;
        setFood(f);
        setServingId(f.default_serving_id);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(
          e instanceof ApiError && e.status === 404
            ? "Food not found."
            : "Couldn't load this food.",
        );
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (!food) {
    return (
      <main className="shell">
        <header className="brand">
          <BackBar parent="/library" label="Library" />
        </header>
        <p className="m3-empty">{error ?? "Loading…"}</p>
      </main>
    );
  }

  const servings = food.servings ?? [];
  const chosen =
    servings.find((s) => s.serving_id === servingId) ?? servings[0] ?? null;
  const macros: Record<string, number | undefined> = chosen?.macros ?? {};
  const payload = (food.payload ?? {}) as Record<string, unknown>;
  const dataType = typeof payload.dataType === "string" ? payload.dataType : null;
  const published =
    typeof payload.publishedDate === "string" ? payload.publishedDate : null;
  const badge = sourceBadge(food.source);
  const link = sourceLink(food);
  const citations = (food.provenance?.citations ?? []).filter(
    (c) => c.url && c.url !== food.source_url,
  );
  const extras = Object.entries(macros).filter(
    ([k, v]) => !CORE.has(k) && !KNOWN.has(k) && typeof v === "number",
  );
  const aliases = food.aliases ?? [];
  const nutrients = food.nutrients ?? [];

  return (
    <main className="shell">
      {/* The sticky bar carries only the way back: a food's name can run to
          several lines, too much to pin over the page it describes. */}
      <header className="brand">
        <BackBar parent="/library" label="Library" />
      </header>

      <header className="fd-head">
        <h1>{foodName(food.display_name)}</h1>
        <div className="fd-meta">
          {food.brand ? <span>{food.brand}</span> : null}
          <span className={`badge ${badge.cls}`}>{badge.label}</span>
          {dataType ? <span>{dataType}</span> : null}
          {food.state ? <span className="badge state">{food.state}</span> : null}
          {published ? <span>published {published}</span> : null}
        </div>
        {food.name && food.name !== food.display_name ? (
          <p className="m3-note">Provider name: {food.name}</p>
        ) : null}
      </header>

      <section className="m3-card" aria-label="Nutrition">
        <div className="fd-kcal num">
          <span>
            <span className="n">
              {typeof macros.kcal === "number" ? fmt.format(macros.kcal) : "—"}
            </span>
            <span className="u">kcal</span>
          </span>
          <span className="m3-card-sub">per {chosen?.description ?? "serving"}</span>
        </div>
        <div className="macro-grid num" style={{ marginBottom: 0 }}>
          <div className="macro-cell protein">
            <div className="v">
              {typeof macros.protein_g === "number" ? `${fmt1.format(macros.protein_g)}g` : "—"}
            </div>
            <div className="k">Protein</div>
          </div>
          <div className="macro-cell">
            <div className="v">
              {typeof macros.carb_g === "number" ? `${fmt1.format(macros.carb_g)}g` : "—"}
            </div>
            <div className="k">Carbs</div>
          </div>
          <div className="macro-cell">
            <div className="v">
              {typeof macros.fat_g === "number" ? `${fmt1.format(macros.fat_g)}g` : "—"}
            </div>
            <div className="k">Fat</div>
          </div>
        </div>
        {servings.length > 1 ? (
          <div className="chips">
            {servings.map((s) => (
              <button
                key={s.serving_id}
                type="button"
                className={`chip${s.serving_id === chosen?.serving_id ? " active" : ""}`}
                onClick={() => setServingId(s.serving_id)}
              >
                {s.description}
              </button>
            ))}
          </div>
        ) : null}
      </section>

      <section className="m3-section" aria-label="Nutrients">
        <h2>
          Nutrients
          <span className="count">per {chosen?.description ?? "serving"}</span>
        </h2>
        <div className="m3-card nutrient-list num">
          {NUTRIENTS.map(([k, label]) => (
            <NutrientLine key={k} k={k} label={label} v={macros[k]} />
          ))}
          {extras.map(([k, v]) => (
            <NutrientLine key={k} k={k} label={humanize(k)} v={v} />
          ))}
        </div>
        <p className="m3-note">
          “Not measured” means the source record has no value for it — never
          zero.
        </p>
        {/* §7.1: the grid above is the readable view; this is literally every
            row the source returned, names and units untouched. */}
        {nutrients.length ? (
          <details className="fd-raw">
            <summary>
              All {nutrients.length} nutrients as the source reported them
            </summary>
            {byBasis(nutrients).map(([basis, rows]) => (
              <div key={basis} style={{ marginTop: 8 }}>
                <div className="edit-label">{basis}</div>
                <div className="m3-card nutrient-list num">
                  {rows.map((n, i) => (
                    <div className="nutrient-line" key={`${basis}:${n.name}:${i}`}>
                      <span className="k">{n.name}</span>
                      <span>
                        {fmt3.format(n.amount)}
                        {n.unit ? ` ${n.unit}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </details>
        ) : null}
      </section>

      {food.per_100g ? (
        <section className="m3-section" aria-label="Per 100 g">
          <h2>Per 100 g</h2>
          <div className="m3-card nutrient-list num">
            {Object.entries(food.per_100g).map(([k, v]) => (
              <NutrientLine
                key={k}
                k={k}
                label={k === "kcal" ? "Calories" : humanize(k)}
                v={v}
              />
            ))}
          </div>
        </section>
      ) : null}

      <section className="m3-section" aria-label="Servings">
        <h2>
          Servings<span className="count num">{servings.length}</span>
        </h2>
        <div className="m3-card nutrient-list num">
          {servings.map((s) => (
            <div className="nutrient-line" key={s.serving_id}>
              <span className="k">
                {s.description}
                {s.serving_id === food.default_serving_id ? " · default" : ""}
              </span>
              <span>
                {s.grams != null ? `${fmt.format(s.grams)} g` : ""}
                {typeof s.macros?.kcal === "number"
                  ? `${s.grams != null ? " · " : ""}${fmt.format(s.macros.kcal)} kcal`
                  : ""}
              </span>
            </div>
          ))}
          {servings.length === 0 ? (
            <p className="subsheet-hint">No serving definitions on this record.</p>
          ) : null}
        </div>
      </section>

      <section className="m3-section" aria-label="Your phrases">
        <h2>
          Your phrases<span className="count num">{aliases.length}</span>
        </h2>
        {aliases.length === 0 ? (
          <p className="m3-empty">You haven&rsquo;t logged this food by name yet.</p>
        ) : (
          <div className="m3-card">
            <div className="chips">
              {aliases.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className="chip"
                  onClick={() => setAliasSheet(a)}
                >
                  {a.pinned ? <span className="usual-dot" /> : null}
                  {a.alias_norm}
                  <span className="alias-count num"> {a.use_count}×</span>
                </button>
              ))}
            </div>
            <p className="m3-note">
              Tap a phrase to pin it, set its default portion, or forget it.
            </p>
          </div>
        )}
      </section>

      <section className="m3-section" aria-label="Source">
        <h2>Source</h2>
        <div className="m3-card">
          <div className="m3-row">
            <span className="k">Database</span>
            <span>
              {badge.label}
              {dataType ? ` · ${dataType}` : ""}
              {food.food_type ? ` · ${food.food_type}` : ""}
            </span>
          </div>
          <div className="m3-row">
            <span className="k">Record id</span>
            <span className="num" style={{ wordBreak: "break-all", textAlign: "right" }}>
              {food.source_id}
            </span>
          </div>
          {published ? (
            <div className="m3-row">
              <span className="k">Published</span>
              <span className="num">{published}</span>
            </div>
          ) : null}
          {food.refreshed_at ? (
            <div className="m3-row">
              <span className="k">Cached</span>
              <span className="num">
                {new Date(food.refreshed_at).toLocaleDateString()}
              </span>
            </div>
          ) : null}
          {link ? (
            <p className="web-cite">
              Source:{" "}
              <a href={link.href} target="_blank" rel="noopener noreferrer">
                {link.label}
              </a>
            </p>
          ) : null}
          {citations.map((c) => (
            <p className="web-cite" key={c.url}>
              Cited:{" "}
              <a href={c.url} target="_blank" rel="noopener noreferrer">
                {c.title || c.url}
              </a>
            </p>
          ))}
        </div>
        {/* no record from the source, no disclosure: it would open onto "null" */}
        {Object.keys(payload).length ? (
          <details className="fd-raw">
            <summary>Raw record from the source</summary>
            <pre>{JSON.stringify(payload, null, 2)}</pre>
          </details>
        ) : null}
      </section>

      {aliasSheet ? (
        <LibraryAliasSheet
          alias={aliasSheet}
          onSaved={(u) => {
            setFood((f) =>
              f
                ? { ...f, aliases: (f.aliases ?? []).map((a) => (a.id === u.id ? u : a)) }
                : f,
            );
            setAliasSheet(null);
          }}
          onDeleted={(aid) => {
            setFood((f) =>
              f ? { ...f, aliases: (f.aliases ?? []).filter((a) => a.id !== aid) } : f,
            );
            setAliasSheet(null);
          }}
          onClose={() => setAliasSheet(null)}
        />
      ) : null}
    </main>
  );
}
