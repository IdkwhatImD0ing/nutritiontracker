"use client";

import { useEffect, useRef, useState } from "react";
import {
  createLog,
  getDiary,
  type DayTotals,
  type LogResponse,
} from "@/lib/api";
import CaptureBar from "./CaptureBar";
import StagingSheet from "./StagingSheet";
import Tracker from "./Tracker";

function localDateISO(): string {
  const d = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export default function Today() {
  const [totals, setTotals] = useState<DayTotals | null>(null);
  const [staging, setStaging] = useState<LogResponse | null>(null);
  const [captureBusy, setCaptureBusy] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  // One client uuid per log (§5 idempotent upsert): a retry of the same text
  // reuses the id so a double-submit can't create two logs.
  const pendingRef = useRef<{ id: string; text: string } | null>(null);

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

  const submit = async (text: string): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    const pending =
      pendingRef.current?.text === text
        ? pendingRef.current
        : { id: crypto.randomUUID(), text };
    pendingRef.current = pending;
    try {
      const log = await createLog({
        id: pending.id,
        text,
        captured_at: new Date().toISOString(),
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        capture_fallback: false,
      });
      pendingRef.current = null;
      setStaging(log);
      return true;
    } catch {
      setCaptureError("Couldn't log that — check your connection and retry.");
      return false;
    } finally {
      setCaptureBusy(false);
    }
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

      {staging ? (
        <StagingSheet
          log={staging}
          onCommitted={(t) => {
            setTotals(t);
            setStaging(null);
          }}
          onClose={() => setStaging(null)}
        />
      ) : null}
    </main>
  );
}
