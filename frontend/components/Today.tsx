"use client";

import { useEffect, useRef, useState } from "react";
import {
  createLog,
  createLogAudio,
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
import type { VoiceClip } from "./useVoiceCapture";

const UNDO_MS = 6_000;
/** BACKLOG 31: pending stragglers survive an app close so PendingItems can
 *  真 retry-on-open — commit-item is idempotent, so a replayed item is safe. */
const PENDING_KEY = "usual.pending.v1";

type PendingState = { logId: string; items: StagedItem[] };

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
  const [pending, setPending] = useState<PendingState | null>(null);
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
  // §8.1d/§13: a failed voice capture is reported on the NEXT text log as
  // capture_fallback + capture_error — the §9 voice-viability counter.
  const voiceFailRef = useRef<string | null>(null);
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
    // BACKLOG 31: restore persisted stragglers so the retry actually happens
    // on the next open, as the old comment only claimed.
    try {
      const raw = localStorage.getItem(PENDING_KEY);
      if (raw) {
        const p = JSON.parse(raw) as PendingState;
        if (
          p != null &&
          typeof p.logId === "string" &&
          Array.isArray(p.items) &&
          p.items.length > 0
        ) {
          setPending(p);
        } else {
          localStorage.removeItem(PENDING_KEY);
        }
      }
    } catch {
      /* corrupt or unavailable storage — start clean */
    }
    return () => {
      cancelled = true;
    };
  }, []);

  const updatePending = (p: PendingState | null) => {
    setPending(p);
    try {
      if (p) localStorage.setItem(PENDING_KEY, JSON.stringify(p));
      else localStorage.removeItem(PENDING_KEY);
    } catch {
      /* storage unavailable — in-memory pending still works this session */
    }
  };

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
        // §13: a text log typed after a failed voice capture carries the
        // failure — capture_fallback + the client-detected capture_error.
        capture_fallback: voiceFailRef.current != null,
        capture_error: voiceFailRef.current,
      });
      pendingRef.current = null;
      voiceFailRef.current = null; // reported once, on the fallback log
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

  // §8.1d voice path: upload the validated clip; the response enters the
  // identical staging flow as text. staged_ms measures stop-tap → rendered
  // (§11: "capture stop → staged" is the number staged_ms tracks).
  const submitAudio = async (clip: VoiceClip): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    flushDiscard();
    t0Ref.current = performance.now();
    // A failure from an earlier attempt rides this clip (voice retried and
    // worked) so it is counted once, here — not stamped as a "fallback" on
    // some unrelated text log an hour later.
    const priorFailure = voiceFailRef.current;
    try {
      const res = await createLogAudio({
        id: crypto.randomUUID(),
        clip,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        capture_error: priorFailure,
      });
      if (res.kind === "capture_failure") {
        // Server-side half of §13's silent-blob class: audio uploaded fine
        // but transcribed to nothing. The server recorded it on the audio
        // log; the retype that follows links to it via capture_fallback.
        t0Ref.current = null;
        voiceFailRef.current = "empty_transcript";
        return false;
      }
      if (voiceFailRef.current === priorFailure) voiceFailRef.current = null;
      setStaging({ log: res.log, restore: null });
      return true;
    } catch {
      t0Ref.current = null;
      voiceFailRef.current = "upload_failed";
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

      <CaptureBar
        busy={captureBusy}
        error={captureError}
        onSubmit={submit}
        onSubmitAudio={submitAudio}
        onVoiceFailure={(e) => {
          voiceFailRef.current = e;
        }}
      />

      {pending ? (
        <PendingItems
          key={pending.logId}
          logId={pending.logId}
          items={pending.items}
          onTotals={setTotals}
          onDone={() => updatePending(null)}
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
              updatePending({ logId: staging.log.log_id, items: stragglers });
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
