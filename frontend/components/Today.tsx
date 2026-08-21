"use client";

import { useEffect, useRef, useState } from "react";
import {
  createLog,
  discardLog,
  getDiary,
  patchTimings,
  type DayTotals,
  type LogResponse,
  type StagedItem,
} from "@/lib/api";
import CaptureBar from "./CaptureBar";
import PendingItems from "./PendingItems";
import StagingSheet, { type StagingSnapshot } from "./StagingSheet";
import Tracker from "./Tracker";

const UNDO_MS = 6_000;

function localDateISO(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export default function Today() {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [staging, setStaging] = useState<{
    log: LogResponse;
    restore: StagingSnapshot | null;
  } | null>(null);
  const [pending, setPending] = useState<{
    logId: string;
    items: StagedItem[];
  } | null>(null);
  // BACKLOG 11: sheet close discards the log — but through an undo toast, so
  // a fat-fingered backdrop tap doesn't destroy a staged meal. The DELETE
  // only fires when the toast expires un-undone.
  const [discard, setDiscard] = useState<{
    log: LogResponse;
    snap: StagingSnapshot;
  } | null>(null);
  const discardRef = useRef(discard);
  discardRef.current = discard;
  const discardTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [captureBusy, setCaptureBusy] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  // One client uuid per log (§5 idempotent upsert): a retry of the same text
  // reuses the id so a double-submit can't create two logs.
  const pendingRef = useRef<{ id: string; text: string } | null>(null);
  // BACKLOG 2: staged_ms = performance.now() from submit to first sheet
  // render, PATCHed once per log (§9's capture→staged latency series).
  const t0Ref = useRef<number | null>(null);
  const timedRef = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getDiary(localDateISO())
      .then((d) => {
        // GET /api/diary returns {date, meals, totals} (§5) — the tracker
        // consumes the totals block.
        if (!cancelled) setTotals(d.totals);
      })
      .catch(() => {
        /* zero-state renders gracefully without the diary */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const flushDiscard = () => {
    if (discardTimer.current) {
      clearTimeout(discardTimer.current);
      discardTimer.current = null;
    }
    const d = discardRef.current;
    if (d) {
      discardLog(d.log.log_id).catch(() => {});
      setDiscard(null);
    }
  };

  const submit = async (text: string): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    flushDiscard(); // a new capture settles any still-undoable discard
    const pendingLog =
      pendingRef.current?.text === text
        ? pendingRef.current
        : { id: crypto.randomUUID(), text };
    pendingRef.current = pendingLog;
    t0Ref.current = performance.now();
    try {
      const log = await createLog({
        id: pendingLog.id,
        text,
        captured_at: new Date().toISOString(),
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        capture_fallback: false,
      });
      pendingRef.current = null;
      setStaging({ log, restore: null });
      return true;
    } catch {
      t0Ref.current = null;
      setCaptureError("Couldn't log that — check your connection and retry.");
      return false;
    } finally {
      setCaptureBusy(false);
    }
  };

  const stagedRendered = (logId: string) => {
    if (t0Ref.current == null || timedRef.current === logId) return;
    const ms = performance.now() - t0Ref.current;
    t0Ref.current = null;
    timedRef.current = logId;
    patchTimings(logId, ms).catch(() => {
      /* telemetry only — never user-visible */
    });
  };

  const closeStaging = (snap: StagingSnapshot) => {
    const current = staging;
    if (!current) return;
    flushDiscard(); // at most one undoable discard at a time
    setStaging(null);
    const entry = { log: current.log, snap };
    setDiscard(entry);
    discardTimer.current = setTimeout(() => {
      discardTimer.current = null;
      // Undo window passed: the discard lands server-side (status:'discarded'
      // — adherence-relevant data, §5).
      discardLog(entry.log.log_id).catch(() => {});
      setDiscard(null);
    }, UNDO_MS);
  };

  const undoDiscard = () => {
    if (discardTimer.current) {
      clearTimeout(discardTimer.current);
      discardTimer.current = null;
    }
    const d = discardRef.current;
    if (!d) return;
    setDiscard(null);
    setStaging({ log: d.log, restore: d.snap }); // local edits intact
  };

  const today = new Date().toLocaleDateString("en-US", {
    weekday: "long",
    month: "short",
    day: "numeric",
  });

  return (
    <main className="shell">
      <header className="brand">
        <h1>Usual</h1>
        <span className="date">{today}</span>
      </header>

      <Tracker totals={totals} />

      <CaptureBar busy={captureBusy} error={captureError} onSubmit={submit} />

      {pending ? (
        <PendingItems
          key={pending.logId}
          logId={pending.logId}
          items={pending.items}
          onTotals={setTotals}
          onDone={() => setPending(null)}
        />
      ) : null}

      {staging ? (
        <StagingSheet
          log={staging.log}
          restore={staging.restore}
          onRendered={() => stagedRendered(staging.log.log_id)}
          onCommitted={(t, stragglers) => {
            setTotals(t);
            setStaging(null);
            if (stragglers.length > 0) {
              setPending({ logId: staging.log.log_id, items: stragglers });
            }
          }}
          onClose={closeStaging}
        />
      ) : null}

      {discard ? (
        <div className="toast" role="status">
          <span>Log discarded</span>
          <button type="button" onClick={undoDiscard}>
            Undo
          </button>
        </div>
      ) : null}
    </main>
  );
}
