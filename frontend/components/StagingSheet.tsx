"use client";

import { useEffect, useRef, useState } from "react";
import {
  answerQuestion,
  commitLog,
  getLog,
  kickTier3,
  quotaRefusal,
  removeStagedItem,
  resolveItem,
  selectFood,
  type AliasRow,
  type CommitResult,
  type FoodSearchResult,
  type LogResponse,
  type Meal,
  type StagedItem,
} from "@/lib/api";
import { foodName, MEAL_LABEL } from "@/lib/labels";
import FoodEditSheet from "./FoodEditSheet";
import { ArrowUp } from "./icons";
import ManualSearchSheet from "./ManualSearchSheet";
import { readEstimate } from "./PhotoApi";
import PortionSheet, { type PortionChoice } from "./PortionSheet";
import { SheetClose } from "./SheetHeader";
import StagedRow, { type PickOption } from "./StagedRow";
import { useDragDismiss } from "./useDragDismiss";
import "@/app/photo.css";

const MEALS: Meal[] = ["breakfast", "lunch", "dinner", "snack"];
const POLL_FIRST_MS = 1_000;
const POLL_NEXT_MS = 2_000;
const POLL_STOP_MS = 90_000; // hard stop (§5: 1s → 2s backoff, ~90s)
const STALE_MS = 60_000; // §3.4: searching past 60s is stale → re-kick once

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const CORE = ["kcal", "protein_g", "carb_g", "fat_g"] as const;

type MacrosT = NonNullable<StagedItem["macros"]>;

function scaleMacros(
  m: Record<string, number | null | undefined>,
  f: number,
): MacrosT {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(m)) {
    if (typeof v === "number") out[k] = Math.round(v * f * 100) / 100;
  }
  return out as MacrosT;
}

/** The portion sheet's list for a staged row: the payload's servings, else
 *  (older staged payloads without a serving list) the chosen serving alone,
 *  so the qty stepper still works. */
function portionChoices(item: StagedItem): PortionChoice[] {
  const fromPayload = (item.servings ?? [])
    .map((s) => ({
      id: s.serving_id,
      description: s.description,
      macros: s.macros,
    }))
    .filter((s) => s.id);
  if (fromPayload.length) return fromPayload;
  return item.serving
    ? [{ id: item.serving.id, description: item.serving.description }]
    : [];
}

/** Everything needed to restore the sheet unchanged after an undone discard
 *  (BACKLOG 11) — local edits included. */
export type StagingSnapshot = {
  items: StagedItem[];
  meal: Meal;
  removed: number[];
  logMealOverride: Meal | null;
  itemMealOverrides: Record<number, Meal>;
};

type Props = {
  log: LogResponse;
  /** undo-restore state; when set, initial state comes from here, not `log` */
  restore?: StagingSnapshot | null;
  /** item_index values this log had removed before this mount. GET
   *  /api/logs/:id does not return removed rows, so a sheet reopened from a
   *  chat card cannot see the hole its own remove left — and "Add another
   *  food" allocates from what it can see. */
  removedIndexes?: number[];
  /** fires once after first paint — BACKLOG 2's staged_ms measurement point */
  onRendered?: () => void;
  /** `pending` = every row the commit did not land: the §3.5 late stragglers
   *  (still searching), the ones it refused, and the ones that never resolved;
   *  `landed` = the rest, which Today's confirmation toast sums */
  onCommitted: (
    totals: CommitResult,
    pending: StagedItem[],
    landed: StagedItem[],
  ) => void;
  onClose: (snapshot: StagingSnapshot) => void;
  /** Rendered as the upper pane of <SplitSheet> rather than as a sheet of its
   *  own: no backdrop and no rounded shell, so the chat stays visible and
   *  usable underneath (Bill, 2026-09-09). × still closes the staged list —
   *  in the split that collapses the pane and leaves the conversation up. */
  docked?: boolean;
};

export default function StagingSheet({
  log,
  restore,
  removedIndexes,
  onRendered,
  onCommitted,
  onClose,
  docked = false,
}: Props) {
  const [items, setItems] = useState<StagedItem[]>(restore?.items ?? log.items);
  const [meal, setMeal] = useState<Meal>(restore?.meal ?? log.meal);
  const [mealPickerOpen, setMealPickerOpen] = useState(false);
  const [removed, setRemoved] = useState<ReadonlySet<number>>(
    new Set(restore?.removed ?? []),
  );
  const [expanded, setExpanded] = useState<number | null>(null);
  const [busyRows, setBusyRows] = useState<ReadonlySet<number>>(new Set());
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [portionFor, setPortionFor] = useState<number | null>(null);
  const [manualFor, setManualFor] = useState<number | null>(null);
  const [manualBusy, setManualBusy] = useState(false);
  const [editFor, setEditFor] = useState<number | null>(null); // BACKLOG 23
  const [addText, setAddText] = useState("");

  const itemsRef = useRef(items);
  itemsRef.current = items;
  const mealRef = useRef(meal);
  mealRef.current = meal;
  const removedRef = useRef(removed);
  removedRef.current = removed;
  // BACKLOG 8: local meal overrides are merged over every server payload
  // (kick, poll, answer) instead of being clobbered by it. Refs, not state —
  // items already carry the applied meal; these only replay it on merges.
  const mealOverridesRef = useRef<{ log: Meal | null; items: Record<number, Meal> }>({
    log: restore?.logMealOverride ?? null,
    items: { ...(restore?.itemMealOverrides ?? {}) },
  });
  // Kick accounting per item (§3.4 lifecycle: kick once on arrival, re-kick a
  // stale row once, then not_found). Also guards StrictMode double-mount.
  const kickCountRef = useRef<Map<number, number>>(new Map());
  const lastKickRef = useRef<Map<number, number>>(new Map());
  // Rows an inline POST /api/resolve currently owns (add-item / edit-sheet
  // rephrase). The poll and stray kick responses must not touch them: the
  // resolve route runs tier 3 itself when needed, and a stale poll payload
  // for the same index could otherwise resurrect the pre-edit food.
  const resolvingRef = useRef<Set<number>>(new Set());
  const renderedRef = useRef(false);

  const withOverrides = (server: StagedItem): StagedItem => {
    const o = mealOverridesRef.current;
    return {
      ...server,
      meal: o.items[server.item_index] ?? o.log ?? server.meal,
    };
  };

  const mergeItem = (updated: StagedItem) => {
    const applied = withOverrides(updated);
    setItems((prev) =>
      prev.map((i) => (i.item_index === applied.item_index ? applied : i)),
    );
  };

  const kick = (itemIndex: number) => {
    kickCountRef.current.set(
      itemIndex,
      (kickCountRef.current.get(itemIndex) ?? 0) + 1,
    );
    lastKickRef.current.set(itemIndex, Date.now());
    kickTier3({ log_id: log.log_id, item_index: itemIndex })
      .then((item) => {
        // Merge only if the row is still searching — a kick response landing
        // after the poll already resolved the row must not clobber edits the
        // user made in the meantime (and never a row an inline resolve owns).
        if (resolvingRef.current.has(item.item_index)) return;
        const local = itemsRef.current.find(
          (i) => i.item_index === item.item_index,
        );
        if (local?.status === "searching") mergeItem(item);
      })
      .catch((e) => {
        // A demo past its re-resolution cap gets the server's sentence (§5:
        // never a generic error); the row stays `searching` and the poll +
        // watchdog run it down to not_found. Any other failure the poll +
        // watchdog cover on their own.
        const q = quotaRefusal(e);
        if (q) setError(q.detail);
      });
  };

  /** POST /api/resolve for one row (add-item and the §7.1 edit-sheet text
   *  field). The route runs tiers 1–2 and, when needed, tier 3 inline — the
   *  response is final, no client-side kick. `before` is the row as it was
   *  before the edit (null for a freshly added row): a demo-quota 429 spent
   *  nothing server-side, so the sheet shows the server's sentence and puts
   *  the row back (or drops the added one) instead of a not_found row. */
  const resolveInto = (
    itemIndex: number,
    phrase: string,
    before: StagedItem | null,
  ) => {
    resolvingRef.current.add(itemIndex);
    resolveItem({ log_id: log.log_id, item_index: itemIndex, phrase })
      .then((item) => {
        resolvingRef.current.delete(itemIndex);
        // Same guard as kick(): don't clobber edits made after the poll
        // already surfaced this row's resolution.
        const local = itemsRef.current.find(
          (i) => i.item_index === item.item_index,
        );
        if (local == null || local.status === "searching") mergeItem(item);
      })
      .catch((e) => {
        resolvingRef.current.delete(itemIndex);
        const q = quotaRefusal(e);
        if (q) {
          setError(q.detail);
          setItems((prev) =>
            before
              ? prev.map((i) => (i.item_index === itemIndex ? before : i))
              : prev.filter((i) => i.item_index !== itemIndex),
          );
          return;
        }
        setItems((prev) =>
          prev.map((i) =>
            i.item_index === itemIndex
              ? { ...i, status: "not_found" as const }
              : i,
          ),
        );
      });
  };

  // BACKLOG 2: signal first paint exactly once (StrictMode double-mount safe).
  useEffect(() => {
    if (renderedRef.current) return;
    renderedRef.current = true;
    onRendered?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Kick tier 3 once for every row currently `searching` (§3.4: the
  // client-side kick). On an undo-restore or reopened log this re-kicks the
  // stale rows — exactly the §3.4 "log reopened later" behavior.
  useEffect(() => {
    for (const item of itemsRef.current) {
      if (
        item.status === "searching" &&
        (kickCountRef.current.get(item.item_index) ?? 0) === 0
      ) {
        kick(item.item_index);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [log.log_id]);

  // Poll GET /api/logs/:id while anything is searching: 1s then 2s backoff,
  // hard stop at ~90s. The server's watchdog flags stale rows (§5); the
  // client re-kicks a stale row once, then renders not_found. A local 60s
  // since-last-kick timer covers the same lifecycle until the server-side
  // stale flag lands (W3 seam).
  const hasSearching = items.some((i) => i.status === "searching");
  useEffect(() => {
    if (!hasSearching) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const start = Date.now();

    const isStale = (server: StagedItem) => {
      if (server.stale === true) return true;
      const last = lastKickRef.current.get(server.item_index);
      return last != null && Date.now() - last > STALE_MS;
    };

    const tick = (delay: number) => {
      timer = setTimeout(async () => {
        if (cancelled) return;
        if (!itemsRef.current.some((i) => i.status === "searching")) return;
        // The hard stop is a LAST READ, not a verdict of its own. `start` is
        // wall-clock and never restarted, so a phone that spent two minutes in
        // a pocket lands straight on it — while tier 3, which runs server-side,
        // may well have answered in the meantime. Flipping the rows without
        // asking would throw that answer away twice over: once here, and again
        // when the real one lands and resolveInto's merge guard finds the row
        // no longer `searching`.
        const stopping = Date.now() - start > POLL_STOP_MS;
        try {
          const fresh = await getLog(log.log_id);
          if (cancelled) return;
          // Decide re-kicks OUTSIDE the state updater (updaters must stay
          // pure; StrictMode may run them twice).
          const rekicks = itemsRef.current
            .filter(
              (i) =>
                i.status === "searching" &&
                !resolvingRef.current.has(i.item_index),
            )
            .map((i) =>
              fresh.items.find((f) => f.item_index === i.item_index),
            )
            .filter(
              (s): s is StagedItem =>
                s != null &&
                s.status === "searching" &&
                isStale(s) &&
                (kickCountRef.current.get(s.item_index) ?? 0) < 2,
            )
            .map((s) => s.item_index);
          setItems((prev) =>
            prev.map((i) => {
              if (i.status !== "searching") return i; // keep local edits
              if (resolvingRef.current.has(i.item_index)) return i; // in-flight /api/resolve owns it
              const server = fresh.items.find(
                (f) => f.item_index === i.item_index,
              );
              if (!server) return i;
              const merged = withOverrides(server);
              if (
                server.status === "searching" &&
                (stopping ||
                  (isStale(server) &&
                    (kickCountRef.current.get(i.item_index) ?? 0) >= 2))
              ) {
                // Re-kicked once already, or out of time — §3.4: not_found,
                // and now only for a row the server itself still calls
                // `searching`.
                return { ...merged, status: "not_found" as const, stale: null };
              }
              return merged;
            }),
          );
          if (!stopping) rekicks.forEach(kick);
        } catch {
          /* transient poll failure — keep going until the stop */
          if (stopping && !cancelled) {
            // Out of time with the last read unanswered: there is nothing to
            // merge, but no item may spin forever (§3.4). A row an inline
            // /api/resolve owns is still that call's to settle, exactly as
            // the merge above leaves it alone.
            setItems((prev) =>
              prev.map((i) =>
                i.status === "searching" &&
                !resolvingRef.current.has(i.item_index)
                  ? { ...i, status: "not_found" as const }
                  : i,
              ),
            );
          }
        }
        if (stopping) return;
        tick(POLL_NEXT_MS);
      }, delay);
    };

    tick(POLL_FIRST_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasSearching, log.log_id]);

  const setRowBusy = (itemIndex: number, busy: boolean) =>
    setBusyRows((prev) => {
      const next = new Set(prev);
      if (busy) next.add(itemIndex);
      else next.delete(itemIndex);
      return next;
    });

  // Question options, alternate chips, and the cooked/raw toggle go through
  // /api/resolve/answer: id-bearing picks resolve locally — one lookup, no
  // second agent run (§3.4/§7.1); a cooked/raw toggle on a resolved row also
  // resolves locally server-side (at most one targeted counterpart search,
  // §3.4a); other id-less labels re-enter tier 3 once as free-text answers.
  const pick = async (item: StagedItem, opt: PickOption) => {
    setRowBusy(item.item_index, true);
    setError(null);
    try {
      const answer =
        opt.food_id || (opt.source && opt.source_id)
          ? {
              label: opt.label,
              source: opt.source,
              source_id: opt.source_id,
              food_id: opt.food_id,
            }
          : { text: opt.label };
      const updated = await answerQuestion({
        log_id: log.log_id,
        item_index: item.item_index,
        answer,
      });
      mergeItem(updated);
    } catch (e) {
      // a demo past its re-resolution cap: the server's sentence, verbatim
      setError(quotaRefusal(e)?.detail ?? "Couldn't update that item.");
    } finally {
      setRowBusy(item.item_index, false);
    }
  };

  const pickMeal = (m: Meal) => {
    // Log-level chip propagates to ALL rows — searching ones included — and
    // survives kick/poll refreshes via the override merge (§7.1, BACKLOG 8).
    setMeal(m);
    setMealPickerOpen(false);
    mealOverridesRef.current = { log: m, items: {} };
    setItems((prev) => prev.map((i) => ({ ...i, meal: m })));
  };

  const pickItemMeal = (itemIndex: number, m: Meal) => {
    mealOverridesRef.current.items[itemIndex] = m;
    setItems((prev) =>
      prev.map((i) => (i.item_index === itemIndex ? { ...i, meal: m } : i)),
    );
  };

  const remove = (itemIndex: number) => {
    setRemoved((prev) => new Set(prev).add(itemIndex));
    // §7.1: removal writes item_resolutions.status='removed' server-side so
    // the decompose error stays visible in telemetry. Commit-time absence is
    // the backstop, so a failure here (route not deployed yet) is swallowed.
    removeStagedItem(log.log_id, itemIndex).catch(() => {});
  };

  // §7.1 "Add another food": goes through POST /api/resolve with the log
  // context so added items get resolution telemetry like everything else.
  const addItem = () => {
    const phrase = addText.trim();
    if (!phrase) return;
    // The index has to clear every index this log has EVER used, removed rows
    // included: the server keeps a removed row under its index, refuses to
    // re-resolve it (409 "item already removed"), and then refuses the WHOLE
    // commit for naming it (§3.5, removed rows are never resurrected). A row
    // removed in THIS mount is still in `items` — only `visible` hides it —
    // so the gap that has to be closed is the one an earlier mount left.
    const idx =
      Math.max(
        -1,
        ...itemsRef.current.map((i) => i.item_index),
        ...(removedIndexes ?? []),
      ) + 1;
    const m = mealRef.current;
    mealOverridesRef.current.items[idx] = m; // added rows keep the sheet's meal
    setItems((prev) => [
      ...prev,
      {
        item_index: idx,
        phrase,
        status: "searching" as const,
        meal: m,
        estimated: "none" as const,
      },
    ]);
    setAddText("");
    resolveInto(idx, phrase, null);
  };

  // §7.1 food bullet (BACKLOG 23) — the three edit-sheet exits.
  const editPickAlias = (a: AliasRow) => {
    if (editFor == null) return;
    const item = itemsRef.current.find((i) => i.item_index === editFor);
    setEditFor(null);
    // Rides /api/resolve/answer with food_id: resolves locally from the cache
    // and records the explicit choice (§3.2 rule 2 — flips the next default).
    if (item) void pick(item, { label: a.food_name, food_id: a.food_id });
  };

  const editRephrase = (text: string) => {
    if (editFor == null) return;
    const idx = editFor;
    setEditFor(null);
    const before =
      itemsRef.current.find((i) => i.item_index === idx) ?? null;
    // Full row reset: the new phrase replaces the old resolution wholesale
    // (server-side too — /api/resolve rewrites the staged payload).
    setItems((prev) =>
      prev.map((i) =>
        i.item_index === idx
          ? {
              item_index: idx,
              phrase: text,
              status: "searching" as const,
              meal: i.meal,
              estimated: "none" as const,
            }
          : i,
      ),
    );
    resolveInto(idx, text, before);
  };

  const editManualSearch = () => {
    if (editFor == null) return;
    const idx = editFor;
    setEditFor(null);
    setManualFor(idx);
  };

  // §7.1 portion sheet apply: optimistic client-side; display macros rescale
  // when derivable, otherwise render "—" (the commit snapshot always
  // re-derives from the foods cache server-side, so display can't corrupt).
  const applyPortion = (
    itemIndex: number,
    serving: PortionChoice,
    qty: number,
  ) => {
    setItems((prev) =>
      prev.map((i) => {
        if (i.item_index !== itemIndex) return i;
        let macros: StagedItem["macros"] = null;
        if (CORE.every((k) => typeof serving.macros?.[k] === "number")) {
          macros = scaleMacros(serving.macros as Record<string, number>, qty);
        } else if (
          i.serving?.id === serving.id &&
          i.macros != null &&
          i.qty != null &&
          i.qty > 0
        ) {
          macros = scaleMacros(i.macros, qty / i.qty);
        }
        return {
          ...i,
          serving: { id: serving.id, description: serving.description },
          qty,
          macros,
        };
      }),
    );
    setPortionFor(null);
  };

  // BACKLOG 3: manual-search selection round-trips through the server
  // (POST /api/foods/select, or /api/resolve/answer for cached foods) so
  // item_resolutions records the resolution like any other path.
  const manualSelect = async (r: FoodSearchResult) => {
    if (manualFor == null) return;
    setManualBusy(true);
    setError(null);
    try {
      const updated = await selectFood({
        log_id: log.log_id,
        item_index: manualFor,
        hit: r,
      });
      mergeItem(updated);
      setManualFor(null);
    } catch {
      setError("Couldn't select that food.");
    } finally {
      setManualBusy(false);
    }
  };

  const snapshot = (): StagingSnapshot => ({
    items: itemsRef.current,
    meal: mealRef.current,
    removed: [...removedRef.current],
    logMealOverride: mealOverridesRef.current.log,
    itemMealOverrides: { ...mealOverridesRef.current.items },
  });

  const close = () => onClose(snapshot());
  // a pull on the head closes exactly as the ✕ does (Today's closeStaging)
  const drag = useDragDismiss(close);

  const visible = items.filter((i) => !removed.has(i.item_index));
  const totalKcal = visible.reduce((sum, i) => sum + (i.macros?.kcal ?? 0), 0);
  // 2026-09-09: an estimated row no longer blocks the button — the commit is
  // the confirmation. What still cannot commit is a NUMBERLESS estimate row
  // (no database match, so no macros): the server's core-macros guard refuses
  // that one item and lets the rest through, so this is a heads-up rather
  // than a gate — the row itself already offers "search instead".
  const numberless = visible.filter(
    (i) => i.status === "resolved" && readEstimate(i).estimated !== "none" && i.macros == null,
  ).length;
  const searchFirst =
    numberless === 0
      ? null
      : `Pick a food with Search for the unmatched ${numberless === 1 ? "row" : `${numberless} rows`}, or it won't be counted.`;

  const commit = async () => {
    setCommitting(true);
    setError(null);
    try {
      // Every non-removed row goes in the payload whatever its status —
      // commit treats absent rows as removed and keeps `searching` rows
      // uncommitted for the §3.5 commit-item late path.
      const payload = visible;
      const totals = await commitLog(log.log_id, payload);
      // §3.5: a 200 with per-item errors is a PARTIAL commit — the server
      // flipped those rows to needs_input and kept them uncommitted. Hand
      // them to PendingItems as needs-attention alongside the searching
      // stragglers instead of silently closing over them.
      const refused = new Set((totals.errors ?? []).map((e) => e.item_index));
      // EVERY row the commit did not land goes over, not just the searching
      // and refused ones. A row left `needs_input` (an unanswered question
      // chip) or `not_found` is skipped server-side by design, with no errors
      // entry to report it, so forwarding those two statuses is the only
      // signal there will ever be: without it the sheet closes over a food
      // with nothing mounted to chase it. PendingItems surfaces a
      // non-searching row at once as a needs-attention pill — §7.1 says such
      // a row must not block the commit of the rest, not that it may vanish.
      onCommitted(
        totals,
        payload
          .filter((i) => i.status !== "resolved" || refused.has(i.item_index))
          .map((i) =>
            refused.has(i.item_index)
              ? { ...i, status: "needs_input" as const }
              : i,
          ),
        payload.filter((i) => i.status === "resolved" && !refused.has(i.item_index)),
      );
    } catch {
      setError("Couldn't log this — nothing was saved. Try again.");
      setCommitting(false);
    }
  };

  const portionItem =
    portionFor != null
      ? items.find((i) => i.item_index === portionFor)
      : undefined;
  const manualItem =
    manualFor != null
      ? items.find((i) => i.item_index === manualFor)
      : undefined;
  const editItem =
    editFor != null ? items.find((i) => i.item_index === editFor) : undefined;

  // The staged batch's own controls. They sit in the sheet header standalone
  // and on the tray's lip when docked — one definition, so the two placements
  // cannot drift.
  const mealChip = (
    <button
      type="button"
      className="meal-chip"
      aria-expanded={mealPickerOpen}
      onClick={() => setMealPickerOpen((o) => !o)}
    >
      {MEAL_LABEL[meal]}
    </button>
  );
  const discardBtn = (
    <SheetClose label="Discard these items" onClick={close} />
  );
  const mealPicker = mealPickerOpen ? (
    <div className="meal-picker m3-seg">
      {MEALS.map((m) => (
        <button
          key={m}
          type="button"
          className={m === meal ? "active" : undefined}
          onClick={() => pickMeal(m)}
        >
          {MEAL_LABEL[m]}
        </button>
      ))}
    </div>
  ) : null;

  return (
    <>
      {docked ? null : <div className="sheet-backdrop" onClick={close} />}
      <div
        className={docked ? "split-pane stage-pane" : "sheet"}
        role="dialog"
        aria-label="Items to review"
      >
        {docked ? null : (
          <div className="sheet-head" {...drag}>
            <div className="grabber" />
            <div className="sheet-head-row">
              <span className="sheet-title">Review</span>
              {mealChip}
              {discardBtn}
            </div>
            {mealPicker}
          </div>
        )}

        <div className="sheet-body">
          {visible.map((item) => (
            <StagedRow
              key={item.item_index}
              item={item}
              expanded={expanded === item.item_index}
              busy={busyRows.has(item.item_index)}
              onToggle={() =>
                setExpanded((cur) =>
                  cur === item.item_index ? null : item.item_index,
                )
              }
              onRemove={() => remove(item.item_index)}
              onPick={(opt) => pick(item, opt)}
              onPortion={() => setPortionFor(item.item_index)}
              onManualSearch={() => setManualFor(item.item_index)}
              onMeal={(m) => pickItemMeal(item.item_index, m)}
              onEditFood={() => setEditFor(item.item_index)}
            />
          ))}
          {visible.length === 0 ? (
            <p className="zero-hint" style={{ marginTop: 24 }}>
              Nothing left to log.
            </p>
          ) : null}

          <form
            className="add-item"
            onSubmit={(e) => {
              e.preventDefault();
              addItem();
            }}
          >
            <input
              value={addText}
              onChange={(e) => setAddText(e.target.value)}
              placeholder="Add another food"
              aria-label="Add another food"
              autoComplete="off"
            />
            <button type="submit" disabled={!addText.trim()} aria-label="Add">
              <ArrowUp strokeWidth={2.4} />
            </button>
          </form>
        </div>

        <div className="sheet-foot">
          {error ? <p className="sheet-error">{error}</p> : null}
          {searchFirst && !error ? (
            <p className="sheet-reason">{searchFirst}</p>
          ) : null}
          {docked ? mealPicker : null}
          <div className={docked ? "stage-lip" : undefined}>
            {docked ? discardBtn : null}
            {docked ? mealChip : null}
            <button
              type="button"
              className={docked ? "commit-pill" : "commit-btn"}
              disabled={committing || visible.length === 0}
              onClick={commit}
            >
              {committing ? (
                "Logging…"
              ) : (
                <span className="num">Log · {fmt.format(totalKcal)} kcal</span>
              )}
            </button>
          </div>
        </div>
      </div>

      {portionItem ? (
        <PortionSheet
          title={foodName(portionItem.food?.name ?? portionItem.phrase)}
          servings={portionChoices(portionItem)}
          initialServingId={portionItem.serving?.id ?? null}
          initialQty={portionItem.qty ?? 1}
          // the row's display kcal previews a serving the payload has no
          // macros for (same serving only)
          initialKcal={portionItem.qty ? portionItem.macros?.kcal : null}
          onApply={(serving, qty) =>
            applyPortion(portionItem.item_index, serving, qty)
          }
          onClose={() => setPortionFor(null)}
        />
      ) : null}

      {manualItem ? (
        <ManualSearchSheet
          phrase={manualItem.phrase}
          busy={manualBusy}
          onSelect={manualSelect}
          onClose={() => setManualFor(null)}
        />
      ) : null}

      {editItem ? (
        <FoodEditSheet
          item={editItem}
          busy={busyRows.has(editItem.item_index)}
          onPickAlias={editPickAlias}
          onRephrase={editRephrase}
          onManualSearch={editManualSearch}
          onClose={() => setEditFor(null)}
        />
      ) : null}
    </>
  );
}
