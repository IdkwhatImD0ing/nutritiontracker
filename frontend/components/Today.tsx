"use client";

import { useEffect, useRef, useState } from "react";
import {
  createLog,
  createLogAudio,
  discardLog,
  getDiary,
  patchTimings,
  quotaRefusal,
  type DayTotals,
  type FoodSearchResult,
  type LogCreateResponse,
  type LogResponse,
  type StagedItem,
} from "@/lib/api";
import { ADD_EVENT, ADD_HANDOFF_KEY, isAddLane, type AddLane } from "./AddSheet";
import BarcodeScanner from "./BarcodeScanner";
import DemoBanner from "./DemoBanner";
import type { DiaryMeals } from "./DiaryApi";
import DiaryList from "./DiaryList";
import { cacheFood } from "./LibraryApi";
import ManualSearchSheet from "./ManualSearchSheet";
import { getMe, type Me } from "./MeApi";
import PendingItems from "./PendingItems";
import PhotoCapture from "./PhotoCapture";
import { createLogFromFood, lookupBarcode } from "./QuickAddApi";
import StagingSheet, { type StagingSnapshot } from "./StagingSheet";
import Tracker from "./Tracker";
import type { VoiceClip } from "./useVoiceCapture";
import VoiceSheet from "./VoiceSheet";

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
  // §7.4 Today: the diary grouped by meal renders under the tracker.
  const [meals, setMeals] = useState<DiaryMeals | null>(null);
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
  // Demo accounts (GET /api/me): the banner's quota, re-read after every
  // log attempt so "next log at …" shows the moment the day's log is spent.
  // Owner → demo null → no banner. A failed read renders nothing: the
  // server enforces the quota; the banner only explains it.
  const [me, setMe] = useState<Me | null>(null);
  const demo = me?.demo ?? null;
  const quotaSpent = demo != null && demo.used_today >= demo.limit;
  // Owner ruling (2026-09-02): the capture bar left the Today screen; every
  // way to log opens from the tab bar's + sheet. `lane` is which lane's sheet
  // is up. The pick arrives from TabBar as a window event (Today mounted) or
  // a sessionStorage handoff (navigated here from another tab).
  const [lane, setLane] = useState<AddLane | null>(null);
  // The photo lane owns its own sheet (PhotoCapture: pick → downscale →
  // upload); the staged log then enters the same staging sheet as the rest.
  const [photoOpen, setPhotoOpen] = useState(false);
  // Search/barcode twin of pendingRef: one uuid per food, so a retried
  // from-food log reuses it (§5 idempotent upsert).
  const quickRef = useRef<{ id: string; foodId: string } | null>(null);
  // §8.4: "save this as my usual breakfast" said after logging resolves to
  // zero items — the server saved (or explained) it, so confirm that instead
  // of opening an empty staging sheet.
  const [savedNote, setSavedNote] = useState<string | null>(null);
  // One client uuid per log (§5 idempotent upsert): a retry of the same text
  // reuses the id so a double-submit can't create two logs.
  const pendingRef = useRef<{ id: string; text: string } | null>(null);
  // Voice twin of pendingRef: one uuid per capture attempt, keyed by the
  // clip's capture start, so a retried upload of the same clip reuses the
  // id (§5 idempotent upsert) instead of minting a fresh one per attempt.
  const audioRef = useRef<{ id: string; capturedAt: string } | null>(null);
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
        // consumes the totals block, the diary list the meals.
        if (cancelled) return;
        setTotals(d.totals);
        setMeals(d.meals);
      })
      .catch(() => {
        /* zero-state renders gracefully without the diary */
      });
    getMe()
      .then((m) => {
        if (!cancelled) setMe(m);
      })
      .catch(() => {
        /* no banner — the quota is enforced server-side regardless */
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

  const pickLane = (next: AddLane) => {
    setCaptureError(null);
    if (next === "photo") {
      setLane(null);
      flushDiscard(); // a new capture settles any still-undoable discard
      setPhotoOpen(true);
      return;
    }
    setPhotoOpen(false);
    setLane(next);
  };
  // The + listener is bound once; the ref keeps it on the current closure.
  const pickRef = useRef(pickLane);
  pickRef.current = pickLane;

  useEffect(() => {
    const onAdd = (e: Event) =>
      pickRef.current((e as CustomEvent<AddLane>).detail);
    window.addEventListener(ADD_EVENT, onAdd);
    // Picked on another tab: TabBar stashed the lane and navigated here.
    try {
      const stashed = sessionStorage.getItem(ADD_HANDOFF_KEY);
      if (stashed != null) {
        sessionStorage.removeItem(ADD_HANDOFF_KEY);
        if (isAddLane(stashed)) pickRef.current(stashed);
      }
    } catch {
      /* storage unavailable — the + is one tap away again */
    }
    return () => window.removeEventListener(ADD_EVENT, onAdd);
  }, []);

  // After a commit / late commit-item the diary has new rows — re-read it
  // (the commit response carries totals only).
  const reloadDiary = () => {
    getDiary(localDateISO())
      .then((d) => {
        setTotals(d.totals);
        setMeals(d.meals);
      })
      .catch(() => {
        /* totals from the commit response already rendered */
      });
  };

  const updatePending = (p: PendingState | null) => {
    setPending(p);
    try {
      if (p) localStorage.setItem(PENDING_KEY, JSON.stringify(p));
      else localStorage.removeItem(PENDING_KEY);
    } catch {
      /* storage unavailable — in-memory pending still works this session */
    }
  };

  const refreshMe = () => {
    getMe().then(setMe).catch(() => {});
  };

  // A demo 429 carries the server's own sentence and the reset instant: the
  // sentence goes in the lane's error surface verbatim (never the generic
  // line), the instant into the banner — no round trip. Returns whether the
  // failure was the quota. `fallback` null keeps captureError clear, for a
  // lane that words its own non-quota failures (voice).
  const failCapture = (e: unknown, fallback: string | null): boolean => {
    const q = quotaRefusal(e);
    setCaptureError(q?.detail ?? fallback);
    if (!q) return false;
    setMe((m) =>
      m?.demo
        ? {
            ...m,
            demo: {
              ...m.demo,
              used_today: Math.max(m.demo.used_today, m.demo.limit),
              resets_at: q.resetsAt ?? m.demo.resets_at,
            },
          }
        : m,
    );
    return true;
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

  // §8.4: a log that staged nothing but carried a save outcome is a
  // "save as my usual …" utterance, not a meal — confirm it and stage nothing.
  const enterStaging = (log: LogCreateResponse): void => {
    if (demo) refreshMe(); // the attempt counted — the banner learns it
    if (log.items.length === 0 && (log.saved_meal || log.note)) {
      setSavedNote(
        log.saved_meal
          ? `Saved as "${log.saved_meal.name}" · ${log.saved_meal.item_count} items`
          : (log.note as string),
      );
      return;
    }
    setSavedNote(null);
    setStaging({ log, restore: null });
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
      setLane(null);
      enterStaging(log);
      return true;
    } catch (e) {
      t0Ref.current = null;
      failCapture(e, "Couldn't log that — check your connection and retry.");
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
    const attempt =
      audioRef.current?.capturedAt === clip.capturedAt
        ? audioRef.current
        : { id: crypto.randomUUID(), capturedAt: clip.capturedAt };
    audioRef.current = attempt;
    try {
      const res = await createLogAudio({
        id: attempt.id,
        clip,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        capture_error: priorFailure,
      });
      // The server answered (staged or capture_failure) — the attempt is
      // spent; only a failed upload keeps the id for a retry.
      audioRef.current = null;
      if (res.kind === "capture_failure") {
        // Server-side half of §13's silent-blob class: audio uploaded fine
        // but transcribed to nothing. The server recorded it on the audio
        // log; the retype that follows links to it via capture_fallback.
        t0Ref.current = null;
        voiceFailRef.current = "empty_transcript";
        return false;
      }
      if (voiceFailRef.current === priorFailure) voiceFailRef.current = null;
      setLane(null);
      enterStaging(res.log);
      return true;
    } catch (e) {
      t0Ref.current = null;
      // The demo quota refusing the upload is not a voice failure: §13's
      // counter stays clean, and the sheet shows the server's sentence
      // instead of "type it instead" (typing meets the same wall).
      if (!failCapture(e, null)) voiceFailRef.current = "upload_failed";
      return false;
    } finally {
      setCaptureBusy(false);
    }
  };

  // + sheet Search / Scan barcode lanes: one resolved row from a cached food
  // (POST /api/logs/from-food), then the identical staging flow. Nothing is
  // spoken, so these rows never learn (commit reads logs.input_mode).
  // staged_ms here measures the pick → rendered.
  const quickAdd = async (
    foodId: string,
    inputMode: "barcode" | "search",
  ): Promise<void> => {
    setCaptureBusy(true);
    setCaptureError(null);
    flushDiscard();
    const attempt =
      quickRef.current?.foodId === foodId
        ? quickRef.current
        : { id: crypto.randomUUID(), foodId };
    quickRef.current = attempt;
    t0Ref.current = performance.now();
    try {
      const log = await createLogFromFood({
        id: attempt.id,
        food_id: foodId,
        serving_id: null,
        qty: null,
        meal_hint: null,
        input_mode: inputMode,
        captured_at: new Date().toISOString(),
      });
      quickRef.current = null;
      setLane(null);
      enterStaging(log);
    } catch (e) {
      t0Ref.current = null;
      failCapture(e, "Couldn't log that — check your connection and retry.");
    } finally {
      setCaptureBusy(false);
    }
  };

  // Search lane: an FDC hit is cached first (POST /api/foods/select, the
  // library path) — from-food takes a foods.id, never a raw source id.
  const searchSelect = async (hit: FoodSearchResult): Promise<void> => {
    setCaptureBusy(true);
    setCaptureError(null);
    let foodId: string;
    try {
      foodId = await cacheFood(hit);
    } catch {
      setCaptureBusy(false);
      setCaptureError("Couldn't select that food.");
      return;
    }
    await quickAdd(foodId, "search");
  };

  const scanCode = async (code: string): Promise<void> => {
    setCaptureBusy(true);
    setCaptureError(null);
    let hit: Awaited<ReturnType<typeof lookupBarcode>>;
    try {
      hit = await lookupBarcode(code);
    } catch {
      setCaptureBusy(false);
      setCaptureError("Lookup failed — check your connection and retry.");
      return;
    }
    if (!hit.found) {
      // An honest miss (§13: never fail silently): FDC Branded — the only
      // barcode source wired up; there is no Open Food Facts adapter —
      // doesn't know this code. Say where we looked; the scanner offers search.
      setCaptureBusy(false);
      setCaptureError(
        `${code.replace(/\D/g, "")} isn't in USDA FoodData Central's branded foods.`,
      );
      return;
    }
    await quickAdd(hit.food.id, "barcode");
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
    <main className="shell today">
      <header className="brand">
        <h1>
          <span className="brand-app">Tally</span>
          <span className="brand-page">Today</span>
        </h1>
        <span className="date">{today}</span>
      </header>

      {demo ? <DemoBanner demo={demo} /> : null}

      <Tracker totals={totals} />

      {pending ? (
        <PendingItems
          key={pending.logId}
          logId={pending.logId}
          items={pending.items}
          onTotals={(t) => {
            setTotals(t);
            reloadDiary(); // the straggler is a new diary row
          }}
          onDone={() => updatePending(null)}
        />
      ) : null}

      {meals ? (
        <DiaryList
          meals={meals}
          onPatched={(entry, t) => {
            setTotals(t);
            setMeals((cur) => {
              if (!cur) return cur;
              const next: DiaryMeals = {};
              for (const [meal, list] of Object.entries(cur)) {
                next[meal] = list.map((e) => (e.id === entry.id ? entry : e));
              }
              return next;
            });
          }}
          onDeleted={(id, t) => {
            setTotals(t);
            setMeals((cur) => {
              if (!cur) return cur;
              const next: DiaryMeals = {};
              for (const [meal, list] of Object.entries(cur)) {
                next[meal] = list.filter((e) => e.id !== id);
              }
              return next;
            });
          }}
        />
      ) : null}

      {lane === "voice" ? (
        <VoiceSheet
          busy={captureBusy}
          error={captureError}
          onSubmit={submit}
          onSubmitAudio={submitAudio}
          onVoiceFailure={(e) => {
            voiceFailRef.current = e;
          }}
          onClose={() => setLane(null)}
        />
      ) : null}

      {lane === "search" ? (
        <ManualSearchSheet
          phrase=""
          busy={captureBusy}
          error={captureError}
          onSelect={searchSelect}
          onClose={() => setLane(null)}
        />
      ) : null}

      {lane === "barcode" ? (
        <BarcodeScanner
          busy={captureBusy}
          notice={captureError}
          onCode={scanCode}
          // a spent quota meets the same 429 in the search lane — don't offer it
          onSearchInstead={quotaSpent ? undefined : () => pickLane("search")}
          onClose={() => setLane(null)}
        />
      ) : null}

      {/* §3.4b photo lane: the staged log enters the identical staging flow;
          staged_ms here measures the shot's upload start → rendered, so the
          clock starts in onUploadStart (before the round trip, per attempt),
          not once the staged log is already back. */}
      <PhotoCapture
        open={photoOpen}
        onClose={() => {
          setPhotoOpen(false);
          // the sheet owns its 429 surface; the banner catches up on close
          if (demo) refreshMe();
        }}
        onUploadStart={() => {
          t0Ref.current = performance.now();
        }}
        onStaged={(log) => {
          setPhotoOpen(false);
          enterStaging(log);
        }}
      />

      {staging ? (
        <StagingSheet
          log={staging.log}
          restore={staging.restore}
          onRendered={() => stagedRendered(staging.log.log_id)}
          onCommitted={(t, stragglers) => {
            setTotals(t);
            reloadDiary();
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

      {savedNote ? (
        <div className="toast" role="status">
          <span>{savedNote}</span>
          <button type="button" onClick={() => setSavedNote(null)}>
            OK
          </button>
        </div>
      ) : null}
    </main>
  );
}
