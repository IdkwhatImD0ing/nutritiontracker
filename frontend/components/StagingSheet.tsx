"use client";

import { useEffect, useRef, useState } from "react";
import {
  answerQuestion,
  commitLog,
  getLog,
  kickTier3,
  type DayTotals,
  type LogResponse,
  type Meal,
  type StagedItem,
} from "@/lib/api";
import StagedRow, { type PickOption } from "./StagedRow";

const MEALS: Meal[] = ["breakfast", "lunch", "dinner", "snack"];
const POLL_FIRST_MS = 1_000;
const POLL_NEXT_MS = 2_000;
const POLL_STOP_MS = 90_000; // hard stop (§5: 1s → 2s backoff, ~90s)

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

type Props = {
  log: LogResponse;
  onCommitted: (totals: DayTotals) => void;
  onClose: () => void;
};

export default function StagingSheet({ log, onCommitted, onClose }: Props) {
  const [items, setItems] = useState<StagedItem[]>(log.items);
  const [meal, setMeal] = useState<Meal>(log.meal);
  const [mealPickerOpen, setMealPickerOpen] = useState(false);
  const [removed, setRemoved] = useState<ReadonlySet<number>>(new Set());
  const [expanded, setExpanded] = useState<number | null>(null);
  const [busyRows, setBusyRows] = useState<ReadonlySet<number>>(new Set());
  const [committing, setCommitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const itemsRef = useRef(items);
  itemsRef.current = items;
  const kickedRef = useRef<Set<number>>(new Set());

  const mergeItem = (updated: StagedItem) =>
    setItems((prev) =>
      prev.map((i) => (i.item_index === updated.item_index ? updated : i)),
    );

  const kick = (itemIndex: number) => {
    kickTier3({ log_id: log.log_id, item_index: itemIndex })
      .then(mergeItem)
      .catch(() => {
        /* the poll + 90s watchdog covers a failed kick */
      });
  };

  // Kick tier 3 once for every row that arrived `searching` (§3.4: the
  // client-side kick). kickedRef guards StrictMode double-mount.
  useEffect(() => {
    for (const item of log.items) {
      if (item.status === "searching" && !kickedRef.current.has(item.item_index)) {
        kickedRef.current.add(item.item_index);
        kick(item.item_index);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [log.log_id]);

  // Poll GET /api/logs/:id while anything is searching: 1s then 2s backoff,
  // hard stop at ~90s, after which still-searching rows flip to not_found.
  const hasSearching = items.some((i) => i.status === "searching");
  useEffect(() => {
    if (!hasSearching) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const start = Date.now();

    const tick = (delay: number) => {
      timer = setTimeout(async () => {
        if (cancelled) return;
        if (!itemsRef.current.some((i) => i.status === "searching")) return;
        if (Date.now() - start > POLL_STOP_MS) {
          setItems((prev) =>
            prev.map((i) =>
              i.status === "searching"
                ? { ...i, status: "not_found" as const }
                : i,
            ),
          );
          return;
        }
        try {
          const fresh = await getLog(log.log_id);
          if (cancelled) return;
          setItems((prev) =>
            prev.map((i) => {
              if (i.status !== "searching") return i; // keep local edits
              const server = fresh.items.find(
                (f) => f.item_index === i.item_index,
              );
              return server ?? i;
            }),
          );
        } catch {
          /* transient poll failure — keep going until the stop */
        }
        tick(POLL_NEXT_MS);
      }, delay);
    };

    tick(POLL_FIRST_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [hasSearching, log.log_id]);

  const setRowBusy = (itemIndex: number, busy: boolean) =>
    setBusyRows((prev) => {
      const next = new Set(prev);
      if (busy) next.add(itemIndex);
      else next.delete(itemIndex);
      return next;
    });

  // Question options and alternate chips go through /api/resolve/answer:
  // id-bearing picks resolve locally — one lookup, no second agent run
  // (§3.4/§7.1); an id-less label re-enters tier 3 once as a free-text answer.
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
    } catch {
      setError("Couldn't update that item.");
    } finally {
      setRowBusy(item.item_index, false);
    }
  };

  const pickMeal = (m: Meal) => {
    // Log-level chip propagates to all rows (§7.1).
    setMeal(m);
    setMealPickerOpen(false);
    setItems((prev) => prev.map((i) => ({ ...i, meal: m })));
  };

  const visible = items.filter((i) => !removed.has(i.item_index));
  const totalKcal = visible.reduce((sum, i) => sum + (i.macros?.kcal ?? 0), 0);

  const commit = async () => {
    setCommitting(true);
    setError(null);
    try {
      const totals = await commitLog(log.log_id, visible);
      onCommitted(totals);
    } catch {
      setError("Commit failed — nothing was saved. Try again.");
      setCommitting(false);
    }
  };

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <div className="sheet" role="dialog" aria-label="Staged items">
        <div className="sheet-head">
          <div className="grabber" />
          <div className="sheet-head-row">
            <span className="sheet-title">Staged</span>
            <button
              type="button"
              className="meal-chip"
              onClick={() => setMealPickerOpen((o) => !o)}
            >
              {meal}
            </button>
            <button
              type="button"
              className="sheet-close"
              aria-label="Close"
              onClick={onClose}
            >
              ✕
            </button>
          </div>
          {mealPickerOpen ? (
            <div className="meal-picker">
              {MEALS.map((m) => (
                <button
                  key={m}
                  type="button"
                  className={m === meal ? "active" : undefined}
                  onClick={() => pickMeal(m)}
                >
                  {m}
                </button>
              ))}
            </div>
          ) : null}
        </div>

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
              onRemove={() =>
                setRemoved((prev) => new Set(prev).add(item.item_index))
              }
              onPick={(opt) => pick(item, opt)}
            />
          ))}
          {visible.length === 0 ? (
            <p className="zero-hint" style={{ marginTop: 24 }}>
              Nothing left to commit.
            </p>
          ) : null}
        </div>

        <div className="sheet-foot">
          {error ? <p className="sheet-error">{error}</p> : null}
          <button
            type="button"
            className="commit-btn"
            disabled={committing || visible.length === 0}
            onClick={commit}
          >
            {committing ? (
              "Committing…"
            ) : (
              <span className="num">Commit · {fmt.format(totalKcal)} kcal</span>
            )}
          </button>
        </div>
      </div>
    </>
  );
}
