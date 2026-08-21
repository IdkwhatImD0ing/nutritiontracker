"use client";

import { useEffect, useRef, useState } from "react";
import {
  commitItem,
  getLog,
  kickTier3,
  type DayTotals,
  type StagedItem,
} from "@/lib/api";

const POLL_MS = 2_000;
const STOP_MS = 90_000; // same ~90s ceiling as the staging poll (§5)

type Props = {
  logId: string;
  /** rows that were still `searching` when the log committed (§3.5) */
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
 *  pills: the same silent-hole class, so never dropped without a signal. */
export default function PendingItems({ logId, items, onTotals, onDone }: Props) {
  const [waiting, setWaiting] = useState<StagedItem[]>(
    items.filter((i) => i.status === "searching"),
  );
  const [failed, setFailed] = useState<StagedItem[]>(
    items.filter((i) => i.status !== "searching"),
  );
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
      committingRef.current.add(item.item_index);
      try {
        const totals = await commitItem(logId, item);
        if (cancelled) return;
        onTotals(totals);
        drop(item.item_index);
      } catch {
        committingRef.current.delete(item.item_index);
        if (!cancelled) fail(item); // commit-item is idempotent; next open retries
      }
    };

    const tick = () => {
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
              void land(server);
            } else if (
              server.status === "not_found" ||
              server.status === "needs_input"
            ) {
              // Post-commit questions have no answering UI here — surface the
              // miss honestly instead of spinning.
              fail(server);
            } else if (timedOut) {
              fail(server);
            } else if (server.stale && !rekickedRef.current.has(w.item_index)) {
              // §3.4 watchdog: re-kick a stale row once…
              rekickedRef.current.add(w.item_index);
              kickTier3({ log_id: logId, item_index: w.item_index })
                .then((item) => {
                  if (cancelled) return;
                  if (item.status === "resolved") void land(item);
                })
                .catch(() => {});
            } else if (server.stale) {
              // …then it's a miss (the server flips it to not_found too).
              fail(server);
            }
          }
        } catch {
          /* transient poll failure */
        }
        if (!cancelled && waitingRef.current.length > 0 && !timedOut) tick();
      }, POLL_MS);
    };

    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logId]);

  useEffect(() => {
    if (waiting.length === 0 && failed.length === 0) onDone();
  }, [waiting.length, failed.length, onDone]);

  if (waiting.length === 0 && failed.length === 0) return null;

  return (
    <div className="pending-wrap">
      {waiting.length > 0 ? (
        <div className="pending-pill" role="status">
          <span className="spinner" />
          {waiting.length} pending item{waiting.length > 1 ? "s" : ""}…
        </div>
      ) : null}
      {failed.map((f) => (
        <div key={f.item_index} className="pending-pill failed" role="status">
          <span>&ldquo;{f.phrase}&rdquo; couldn&rsquo;t be added — needs attention</span>
          <button
            type="button"
            aria-label="Dismiss"
            onClick={() =>
              setFailed((prev) =>
                prev.filter((x) => x.item_index !== f.item_index),
              )
            }
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}
