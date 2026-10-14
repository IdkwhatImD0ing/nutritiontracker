"use client";

import { useEffect, useRef, useState } from "react";
import {
  commitItem,
  getLog,
  kickTier3,
  quotaRefusal,
  selectFood,
  type DayTotals,
  type FoodSearchResult,
  type StagedItem,
} from "@/lib/api";
import { Close } from "./icons";
import ManualSearchSheet from "./ManualSearchSheet";
import { readEstimate } from "./PhotoApi";

const POLL_FIRST_MS = 250; // reopen feels immediate (BACKLOG 31 retry-on-open)
const POLL_MS = 2_000;
const STOP_MS = 90_000; // same ~90s ceiling as the staging poll (§5)

type Props = {
  logId: string;
  /** every row the commit did not land (§3.5): still `searching`, refused, or
   *  never resolved at all */
  items: StagedItem[];
  onTotals: (t: DayTotals) => void;
  /** everything landed or was dismissed — parent can drop this component */
  onDone: () => void;
};

/** BACKLOG 12 / §3.5 late stragglers: commit doesn't wait for tier 3. This
 *  badge keeps polling the committed log; when a row resolves it calls
 *  POST /api/logs/:id/commit-item so the item actually reaches the diary —
 *  without this a committed-then-resolved fairlife would silently leave a
 *  ~42g protein hole in the day. Rows the commit REFUSED (core-macros guard,
 *  §3.5 — they arrive here as needs_input) surface immediately as failed
 *  pills: the same silent-hole class, so never dropped without a signal. So do
 *  rows that never resolved — an unanswered question chip, a not_found — which
 *  the commit skips server-side without an errors entry of their own. */
export default function PendingItems({ logId, items, onTotals, onDone }: Props) {
  // `resolved` rows arrive on a reopen (persisted stragglers whose commit-item
  // failed last session) — they retry on mount, not surface as failed.
  const [waiting, setWaiting] = useState<StagedItem[]>(
    items.filter((i) => i.status === "searching" || i.status === "resolved"),
  );
  const [failed, setFailed] = useState<StagedItem[]>(
    items.filter((i) => i.status !== "searching" && i.status !== "resolved"),
  );
  // the failed row being found by hand in the search sub-sheet (findFood)
  const [finding, setFinding] = useState<StagedItem | null>(null);
  const [findBusy, setFindBusy] = useState(false);
  const [findError, setFindError] = useState<string | null>(null);
  const waitingRef = useRef(waiting);
  waitingRef.current = waiting;
  const committingRef = useRef<Set<number>>(new Set());
  const rekickedRef = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const start = Date.now();

    const drop = (itemIndex: number) =>
      setWaiting((prev) => prev.filter((w) => w.item_index !== itemIndex));

    const fail = (item: StagedItem) => {
      drop(item.item_index);
      setFailed((prev) =>
        prev.some((f) => f.item_index === item.item_index)
          ? prev
          : [...prev, item],
      );
    };

    const land = async (item: StagedItem) => {
      if (committingRef.current.has(item.item_index)) return;
      // 2026-09-09: a portion estimate lands like any other row — the commit
      // is the confirmation. A NUMBERLESS estimate row (no database match)
      // still cannot commit and has no way out from here, so say so honestly.
      if (readEstimate(item).estimated !== "none" && item.macros == null) {
        fail(item);
        return;
      }
      committingRef.current.add(item.item_index);
      try {
        const totals = await commitItem(logId, item);
        // Clear the in-flight marker even when this run was cancelled
        // (StrictMode double-mount): commit-item is idempotent, so the next
        // run's retry just gets the stored result — a stale marker would
        // instead block it forever.
        committingRef.current.delete(item.item_index);
        if (cancelled) return;
        onTotals(totals);
        drop(item.item_index);
      } catch {
        committingRef.current.delete(item.item_index);
        if (!cancelled) fail(item); // commit-item is idempotent; retried on next mount
      }
    };

    // BACKLOG 31 — 真 retry-on-open: anything already resolved (a commit-item
    // that failed before this mount, restored via the persisted pending state)
    // retries immediately. Its `meal` already carries the sheet's override.
    for (const w of [...waitingRef.current]) {
      if (w.status === "resolved") void land(w);
    }

    const tick = (delay: number) => {
      timer = setTimeout(async () => {
        if (cancelled || waitingRef.current.length === 0) return;
        const timedOut = Date.now() - start > STOP_MS;
        try {
          const fresh = await getLog(logId);
          if (cancelled) return;
          for (const w of [...waitingRef.current]) {
            const server = fresh.items.find(
              (f) => f.item_index === w.item_index,
            );
            if (!server) {
              fail(w);
            } else if (server.status === "resolved") {
              // BACKLOG 31: the sheet's meal override rides w.meal — the
              // server payload only knows the inferred meal, and commit-item
              // writes entries.meal from the item it's sent.
              void land({ ...server, meal: w.meal });
            } else if (
              server.status === "not_found" ||
              server.status === "needs_input"
            ) {
              // Post-commit questions have no answering UI here — surface the
              // miss honestly instead of spinning. A failed row keeps the
              // sheet's meal too: Find it commits it with that.
              fail({ ...server, meal: w.meal });
            } else if (timedOut) {
              fail({ ...server, meal: w.meal });
            } else if (server.stale && !rekickedRef.current.has(w.item_index)) {
              // §3.4 watchdog: re-kick a stale row once…
              rekickedRef.current.add(w.item_index);
              kickTier3({ log_id: logId, item_index: w.item_index })
                .then((item) => {
                  if (cancelled) return;
                  if (item.status === "resolved")
                    void land({ ...item, meal: w.meal });
                })
                .catch(() => {});
            } else if (server.stale) {
              // …then it's a miss (the server flips it to not_found too).
              fail({ ...server, meal: w.meal });
            }
          }
        } catch {
          /* transient poll failure */
        }
        if (!cancelled && waitingRef.current.length > 0 && !timedOut)
          tick(POLL_MS);
      }, delay);
    };

    tick(POLL_FIRST_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logId]);

  // A failed row is fixed where it stands, not by a new log: the pick
  // resolves this same row (select and answer accept an uncommitted row on a
  // committed log) and commit-item appends it, the late-straggler path. It
  // keeps its meal and back-dated day, and a demo account's one log a day is
  // not spent the way the + sheet's Search lane would spend it.
  const findFood = async (hit: FoodSearchResult) => {
    if (!finding) return;
    const f = finding;
    setFindBusy(true);
    setFindError(null);
    try {
      const item = await selectFood({ log_id: logId, item_index: f.item_index, hit });
      if (item.status !== "resolved") {
        setFindError("Couldn't use that food, try another.");
        return;
      }
      onTotals(await commitItem(logId, { ...item, meal: f.meal }));
      setFailed((prev) => prev.filter((x) => x.item_index !== f.item_index));
      setFinding(null);
    } catch (e) {
      setFindError(quotaRefusal(e)?.detail ?? "Couldn't add that food, try again.");
    } finally {
      setFindBusy(false);
    }
  };

  const idle = waiting.length === 0 && failed.length === 0;
  useEffect(() => {
    if (idle) onDone();
  }, [idle, onDone]);

  if (idle) return null;

  return (
    <div className="pending-wrap">
      {waiting.length > 0 ? (
        <div className="pending-pill" role="status">
          <span className="spinner" />
          {waiting.length === 1 ? (
            <>Adding &ldquo;{waiting[0].phrase}&rdquo;…</>
          ) : (
            `${waiting.length} pending items…`
          )}
        </div>
      ) : null}
      {failed.map((f) => (
        <div key={f.item_index} className="pending-pill failed" role="status">
          <span>Couldn&rsquo;t add &ldquo;{f.phrase}&rdquo;</span>
          <button
            type="button"
            className="pending-find"
            onClick={() => {
              setFindError(null);
              setFinding(f);
            }}
          >
            Find it
          </button>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() =>
              setFailed((prev) =>
                prev.filter((x) => x.item_index !== f.item_index),
              )
            }
          >
            <Close />
          </button>
        </div>
      ))}
      {finding ? (
        <ManualSearchSheet
          phrase={finding.phrase}
          busy={findBusy}
          error={findError}
          onSelect={findFood}
          onClose={() => setFinding(null)}
        />
      ) : null}
    </div>
  );
}
