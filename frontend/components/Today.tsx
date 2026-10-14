"use client";

import { useEffect, useRef, useState } from "react";
import {
  discardLog,
  getDiary,
  patchTimings,
  quotaRefusal,
  streamAgent,
  type AgentEvent,
  type AgentTurn,
  type DayTotals,
  type FoodSearchResult,
  type LogCreateResponse,
  type LogResponse,
  type StagedItem,
} from "@/lib/api";
import { ADD_EVENT, ADD_HANDOFF_KEY, isAddLane, type AddLane } from "./AddSheet";
import AgentChat, { type ChatMessage } from "./AgentChat";
import BarcodeScanner from "./BarcodeScanner";
import DemoBanner from "./DemoBanner";
import { placeEntry, type DiaryMeals } from "./DiaryApi";
import DiaryList from "./DiaryList";
import { cacheFood } from "./LibraryApi";
import ManualSearchSheet from "./ManualSearchSheet";
import { getMe, type Me } from "./MeApi";
import { syncOura } from "./OuraApi";
import PendingItems from "./PendingItems";
import { photoErrorMessage } from "./PhotoApi";
import type { PhotoShot } from "./PhotoCapture";
import { createLogFromFood, lookupBarcode } from "./QuickAddApi";
import SplitSheet from "./SplitSheet";
import StagingSheet, { type StagingSnapshot } from "./StagingSheet";
import Tracker from "./Tracker";
import { useOnForeground } from "./useOnForeground";
import type { VoiceClip } from "./useVoiceCapture";
import WeightCard from "./WeightCard";

const UNDO_MS = 6_000;
/** BACKLOG 31: pending stragglers survive an app close so PendingItems can
 *  真 retry-on-open — commit-item is idempotent, so a replayed item is safe.
 *  Holds a QUEUE, one entry per committed log: two logs committed inside the
 *  same ~90s tier-3 window each need their own poll. */
const PENDING_KEY = "usual.pending.v1";

/** How long Today may sit in the background before a return to the foreground
 *  re-reads the server. Short enough that a meal logged on another device is
 *  there when you look, long enough that flicking to the camera roll and back
 *  costs nothing. A local-date rollover re-reads regardless of this. */
const RESUME_STALE_MS = 60_000;

type PendingState = { logId: string; items: StagedItem[] };

const isPendingState = (p: unknown): p is PendingState =>
  p != null &&
  typeof (p as PendingState).logId === "string" &&
  Array.isArray((p as PendingState).items) &&
  (p as PendingState).items.length > 0;

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
  // §3.5 stragglers, one entry per committed log. A single slot meant the
  // second of two commits inside the tier-3 window overwrote the first, and
  // remounting <PendingItems> under the new key cancelled the first log's
  // poll on unmount — nothing else chases a late row, so it never arrived.
  const [pending, setPending] = useState<PendingState[]>([]);
  const pendingListRef = useRef(pending);
  pendingListRef.current = pending;
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
  // Which item_index values each log has had removed. The server keeps a
  // removed row under its index but leaves it out of GET /api/logs/:id, so a
  // sheet reopened from a chat card cannot see the hole — and its "+ add
  // item" would hand /api/resolve a freed index, which 409s and then refuses
  // the whole commit (§3.5). Remembered here, where the log outlives the
  // sheet, for as long as a card can be reviewed.
  const removedIndexesRef = useRef<Map<string, number[]>>(new Map());
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
  // §3.4b (2026-09-08): the photo lane IS the agent lane now — "Take photo"
  // opens the chat with the picker up, so the plate lands in the conversation
  // that is going to correct it. This is which of the two the lane opened for.
  const [openWith, setOpenWith] = useState<"mic" | "photo">("mic");
  // Search/barcode twin of pendingRef: one uuid per food, so a retried
  // from-food log reuses it (§5 idempotent upsert).
  const quickRef = useRef<{ id: string; foodId: string } | null>(null);
  // §8.4: "save this as my usual breakfast" said after logging resolves to
  // zero items — the server saved (or explained) it, so confirm that instead
  // of opening an empty staging sheet.
  const [savedNote, setSavedNote] = useState<string | null>(null);
  // §6.4 the Agent chat (2026-09-06): the conversation lives here, not in
  // the sheet, so the staging sheet stacked over the chat leaves it intact;
  // the POST /api/agent stream appends to it as events land. Every opening
  // of the lane is a NEW conversation (Bill, 2026-09-08): pickLane clears
  // it, and nothing is kept on the device between openings (the legacy
  // stored copy is dropped app-wide, app/legacy-storage.tsx).
  const [chat, setChat] = useState<ChatMessage[]>([]);
  // Which conversation is open. A run started in one can still be streaming
  // when the next begins — the sheet closes mid-run and the lane is opened
  // again — so every send captures this number and its chat writes are
  // dropped once it is stale: the blank window never fills with the previous
  // conversation's tool lines, reply or result card. The staged log itself
  // still opens the staging sheet (agentDone) — it is real and needs review.
  const convoRef = useRef(0);
  const inConvo = (convo: number) => convoRef.current === convo;
  // The same number as state, purely to key <AgentChat>: a new conversation
  // must REMOUNT it, or its mount effect (which decides between starting the
  // mic and opening the picker) has already latched and the lane's intent is
  // silently dropped — "Take photo" from an open chat would blank the window
  // and do nothing else. Remounting also clears the composer and any recording.
  const [convoKey, setConvoKey] = useState(0);
  const pushChat = (m: ChatMessage) => setChat((cur) => [...cur, m]);
  // a retry of the same turn (§8.6): its bubble stays — put back if the failed
  // attempt dropped it — and the failure note under it goes; the stream the
  // retry re-attaches to then settles the very lines the first attempt showed
  const resumeTurn = (userId: string, bubble: ChatMessage) =>
    setChat((cur) => {
      const at = cur.findIndex((m) => m.id === userId);
      return at < 0 ? [...cur, bubble] : cur.filter((m, i) => !(i > at && m.kind === "error"));
    });
  const patchChat = (id: string, patch: Partial<ChatMessage>) =>
    setChat((cur) =>
      cur.map((m) => (m.id === id ? ({ ...m, ...patch } as ChatMessage) : m)),
    );
  // the result card follows its log: staged → committed / discarded / replaced
  const markResult = (logId: string, state: "committed" | "discarded" | "replaced") =>
    setChat((cur) =>
      cur.map((m) =>
        m.kind === "result" && m.log.log_id === logId ? { ...m, state } : m,
      ),
    );
  // §3.4b: the plate this conversation is still tuning — the newest photo
  // result card that is still staged. DERIVED from the chat, never stored
  // beside it: a separate flag would have to be cleared in every place the
  // card's state changes, and would go stale the moment one was missed. It
  // clears itself, because markResult already flips that card on commit,
  // discard and replacement, and pickLane empties the chat on every opening.
  // `justDiscarded`: the log this send settled a pending discard for — its
  // card still reads staged in this render.
  const photoLogId = (justDiscarded: string | null = null): string | null => {
    for (let i = chat.length - 1; i >= 0; i--) {
      const m = chat[i];
      if (m.kind !== "result" || !m.photo) continue;
      return m.state === "staged" && m.log.log_id !== justDiscarded
        ? m.log.log_id
        : null;
    }
    return null;
  };
  // §3.4b: adjust_photo changed an EARLIER turn's log, so the rows come back
  // on the outcome rather than as this turn's own (empty) log. The card that
  // owns that log shows the new list — a second card would be a lie, and
  // StagingSheet reads its rows from these props when Review mounts it.
  const patchResult = (updated: LogResponse) =>
    setChat((cur) =>
      cur.map((m) =>
        m.kind === "result" && m.log.log_id === updated.log_id
          ? { ...m, log: { ...m.log, ...updated } }
          : m,
      ),
    );
  // a run that ended in an error leaves no tool line spinning
  const settleRunning = () =>
    setChat((cur) =>
      cur.map((m) =>
        m.kind === "tool" && !m.done
          ? { ...m, done: true, output: { error: "interrupted" } }
          : m,
      ),
    );
  // §6.4: the conversation the agent reads — the last turns as the chat
  // remembers them: what was said, what was answered, and what each turn
  // staged with its log id and status (so a correction can name its log).
  // Built BEFORE the new utterance is pushed; the server adds that itself.
  // `justDiscarded`: the log this send settled a pending discard for — its
  // card still reads staged in this render, the agent must hear discarded.
  const chatHistory = (justDiscarded: string | null = null): AgentTurn[] => {
    const turns: AgentTurn[] = [];
    for (const m of chat) {
      if (m.kind === "user" && !m.pending && m.text) {
        turns.push({ role: "user", text: m.text });
      } else if (m.kind === "photo") {
        // the words said WITH the picture are the utterance the agent was
        // answering — dropped, a follow-up ("what about the sauce?") arrives
        // with no record of the question it follows
        turns.push({
          role: "user",
          text: m.text
            ? `[sent a photo of their plate] ${m.text}`
            : "[sent a photo of their plate]",
        });
      } else if (m.kind === "agent") {
        turns.push({ role: "agent", text: m.text });
      } else if (m.kind === "result") {
        const n = m.log.items.length;
        turns.push({
          role: "agent",
          text: `${m.photo ? "read the photo — " : ""}staged ${n} item${n === 1 ? "" : "s"}`,
          log_id: m.log.log_id,
          items: m.log.items.map((it) =>
            `${it.food?.name ?? it.phrase}${
              it.qty != null && it.serving ? ` — ${it.qty} × ${it.serving.description}` : ""
            }`,
          ),
          state:
            m.state === "replaced" || m.log.log_id === justDiscarded
              ? "discarded"
              : m.state,
        });
      }
    }
    const tail = turns.slice(-12);
    // §3.4b: the plate must not fall out of the window it is being tuned in —
    // twelve corrections and the agent would be editing rows it can no longer
    // see. The card it names is the one thing that always rides along.
    const plate = photoLogId(justDiscarded);
    const anchor = plate ? turns.find((t) => t.log_id === plate) : undefined;
    return anchor && !tail.includes(anchor) ? [anchor, ...tail] : tail;
  };
  // a question's chips are for the next message only
  const closeQuestions = () =>
    setChat((cur) =>
      cur.map((m) => (m.kind === "agent" && m.options ? { ...m, options: undefined } : m)),
    );
  // a voice bubble still waiting for its transcript has nothing to keep
  const dropPending = (id: string) =>
    setChat((cur) => cur.filter((m) => !(m.id === id && m.kind === "user" && m.pending)));
  // §6.4: the weight card re-reads GET /api/weights when an agent log
  // recorded a weigh-in (the response says so; the card owns the series).
  const [weightsKey, setWeightsKey] = useState(0);
  // One client uuid per log (§5 idempotent upsert): a retry of the same text
  // reuses the id so a double-submit can't create two logs.
  const pendingRef = useRef<{ id: string; text: string; userId: string } | null>(null);
  // Voice twin of pendingRef: one uuid per capture attempt, keyed by the
  // clip's capture start, so a retried upload of the same clip reuses the
  // id (§5 idempotent upsert) instead of minting a fresh one per attempt.
  const audioRef = useRef<{ id: string; capturedAt: string; userId: string } | null>(null);
  // §3.4b photo twin of audioRef: one uuid per shot, keyed by the capture
  // instant, so the same shot sent twice reuses the id rather than minting a
  // second log for one plate. Note there is no Retry affordance yet — every
  // pick stamps a fresh capturedAt, so a re-picked photo after a failed send
  // IS a new plate to this (BACKLOG 176).
  const photoRef = useRef<{ id: string; capturedAt: string; userId: string } | null>(null);
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
        if (cancelled) return;
        setMe(m);
        syncOuraFor(m);
      })
      .catch(() => {
        /* no banner — the quota is enforced server-side regardless */
      });
    // BACKLOG 31: restore persisted stragglers so the retry actually happens
    // on the next open, as the old comment only claimed.
    try {
      const raw = localStorage.getItem(PENDING_KEY);
      if (raw) {
        // A build before the queue stored one bare object under this key.
        // That is still somebody's un-landed straggler, so it is read as a
        // one-log queue rather than dropped on upgrade.
        const stored = JSON.parse(raw) as unknown;
        const list: unknown[] = Array.isArray(stored) ? stored : [stored];
        const queue = list.filter(isPendingState);
        if (queue.length > 0) setPending(queue);
        else localStorage.removeItem(PENDING_KEY);
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
    // §3.4b (2026-09-08): "Take photo" opens the same chat, with the picker
    // up instead of the mic — so it is the Agent lane, and everything below
    // applies to it. The still-undoable discard is settled by the send
    // (submitPhoto, as submit and submitAudio do), not here.
    const target = next === "photo" ? "agent" : next;
    // §6.4: opening the Agent lane starts a new conversation (Bill,
    // 2026-09-08) — cleared here, on open, rather than on close, so the
    // sheet closing never cuts a run short. The generation bump is what
    // keeps a run still streaming out of the blank window; the per-turn ids
    // go with it, so a repeated utterance is a new log rather than a retry
    // of one the previous conversation left behind (§8.6's same-id replay
    // is for a retry *within* a conversation), and a mic failure is never
    // stamped on a later conversation's first typed log (§13).
    if (target === "agent") {
      convoRef.current += 1;
      setConvoKey(convoRef.current);
      setChat([]);
      pendingRef.current = null;
      audioRef.current = null;
      photoRef.current = null;
      voiceFailRef.current = null;
    }
    setOpenWith(next === "photo" ? "photo" : "mic");
    setLane(target);
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
        /* the tracker keeps what it has — nothing paints another day's numbers
           over it, and the next read (or a foreground) corrects it */
      });
  };

  // The queue is written through the ref, not through a functional setState:
  // it is persisted in the same breath, and a state updater has to stay pure
  // (StrictMode runs it twice).
  const writePending = (next: PendingState[]) => {
    pendingListRef.current = next;
    setPending(next);
    try {
      if (next.length > 0)
        localStorage.setItem(PENDING_KEY, JSON.stringify(next));
      else localStorage.removeItem(PENDING_KEY);
    } catch {
      /* storage unavailable — in-memory pending still works this session */
    }
  };

  // One entry per log, newest last; a log already queued is replaced, never
  // duplicated (React would key two rows the same).
  const addPending = (entry: PendingState) =>
    writePending([
      ...pendingListRef.current.filter((p) => p.logId !== entry.logId),
      entry,
    ]);

  const dropPendingLog = (logId: string) =>
    writePending(pendingListRef.current.filter((p) => p.logId !== logId));

  const refreshMe = () => {
    getMe().then(setMe).catch(() => {});
  };

  // Oura (owner only, services/oura.py): opening or resuming Today is the
  // sync trigger, once Oura is connected (/api/me says so — no request that
  // can only answer 409). The server skips it when the last sync, or failed
  // attempt, is under half an hour old; a failure changes nothing here —
  // Settings shows the state.
  const syncOuraFor = (m: Me | null) => {
    if (m?.is_owner && m.oura_connected) syncOura().catch(() => {});
  };

  // Installed, Tally is resumed far more often than it is loaded: iOS keeps a
  // standalone PWA warm for days, and the effects above run once per mount.
  // Without this the diary, the quota banner and the weight card show whatever
  // they read the first time — and past local midnight, yesterday's day
  // entirely, since `getDiary` is keyed by date (§5). There is no reload button
  // in standalone display and no pull-to-refresh (globals.css turns overscroll
  // off), so nothing else was going to correct it.
  //
  // Re-reading also re-renders, which is what moves the header's date: it is
  // derived during render, not held in state.
  // Lazily, not `useRef(localDateISO())` — a useRef argument is evaluated on
  // every render and thrown away, and Today re-renders on every streamed agent
  // event.
  const dayRef = useRef<string | null>(null);
  if (dayRef.current === null) dayRef.current = localDateISO();
  useOnForeground((awayMs) => {
    const day = localDateISO();
    const rolled = day !== dayRef.current;
    if (!rolled && awayMs < RESUME_STALE_MS) return;
    dayRef.current = day;
    reloadDiary();
    refreshMe();
    syncOuraFor(me);
    // The card owns the series; Today only tells it the world moved (§6.4).
    setWeightsKey((k) => k + 1);
  });

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

  // Settles a still-undoable discard for real; returns the log it discarded
  // so the caller can describe it truthfully (the card's state update has
  // not rendered yet when the next send builds the chat history).
  const flushDiscard = (): string | null => {
    if (discardTimer.current) {
      clearTimeout(discardTimer.current);
      discardTimer.current = null;
    }
    const d = discardRef.current;
    if (d) {
      discardLog(d.log.log_id).catch(() => {});
      markResult(d.log.log_id, "discarded");
      setDiscard(null);
      return d.log.log_id;
    }
    return null;
  };

  // §8.4: a log that staged nothing but carried a save outcome is a
  // "save as my usual …" utterance, not a meal — confirm it and stage nothing.
  // The search and barcode lanes enter here; the Agent chat has its own entry
  // below (its answers are chat messages, not toasts), and the photo lane is
  // part of that chat since 2026-09-08 (§3.4b).
  const enterStaging = (log: LogCreateResponse): void => {
    if (demo) refreshMe(); // the attempt counted — the banner learns it
    if (log.items.length === 0 && (log.saved_meal || log.note)) {
      setSavedNote(
        log.saved_meal
          ? `Saved as "${log.saved_meal.name}" · ${log.saved_meal.item_count} item${log.saved_meal.item_count === 1 ? "" : "s"}`
          : (log.note as string),
      );
      return;
    }
    if (log.items.length === 0) {
      setSavedNote("Nothing was logged — try again.");
      return;
    }
    setSavedNote(null);
    setStaging({ log, restore: null });
  };

  // §6.4: one POST /api/agent event → the chat. The user bubble was pushed
  // before the request (typed text, or a pending one the transcript fills).
  const onAgentEvent = (ev: AgentEvent, userId: string): void => {
    switch (ev.type) {
      case "transcript":
        patchChat(userId, { text: ev.text, pending: false });
        return;
      case "tool_call":
        // a re-attached stream (§8.6) replays the lines the first attempt
        // already showed: the same call id is the same line, running again —
        // with the newest args, which is how the photo turn names the place
        // once its lookup lands (2026-09-12)
        setChat((cur) =>
          cur.some((m) => m.kind === "tool" && m.id === ev.id)
            ? cur.map((m) =>
                m.kind === "tool" && m.id === ev.id
                  ? { ...m, args: ev.args, done: false, output: undefined }
                  : m,
              )
            : [...cur, { kind: "tool", id: ev.id, name: ev.name, args: ev.args, done: false }],
        );
        return;
      case "tool_result":
        // a live web search put its line up before the query was known; the
        // completed item brings it along with the sources
        setChat((cur) =>
          cur.map((m) => {
            if (m.kind !== "tool" || m.id !== ev.id) return m;
            const out = ev.output as Record<string, unknown> | null;
            const query =
              out && typeof out === "object" && typeof out.query === "string" ? out.query : null;
            const args = query && !m.args.query ? { ...m.args, query } : m.args;
            return { ...m, args, output: ev.output, done: true };
          }),
        );
        return;
      case "reply": {
        const options = ev.options?.length ? ev.options : undefined;
        setChat((cur) => {
          // a re-attached stream replays the reply this turn already has: the
          // bubble stays (with its chips back, if the retry had closed them)
          const at = cur.findIndex((m) => m.id === userId);
          const shown =
            at < 0 ? -1 : cur.findIndex((m, i) => i > at && m.kind === "agent" && m.text === ev.text);
          if (shown >= 0) return cur.map((m, i) => (i === shown ? ({ ...m, options } as ChatMessage) : m));
          return [...cur, { kind: "agent", id: crypto.randomUUID(), text: ev.text, options }];
        });
        return;
      }
      default:
        return; // `done` is the promise's result; `error` rejects it
    }
  };

  // §6.4: the stream's `done` — the staged log opens the sheet over the chat
  // (the answer is still the list), a weigh-in refreshes the card, and the
  // §8.4 save outcome is said in the chat. The chat stays open throughout.
  // `replay` (§8.6): the server answered a retry from the stored log — no
  // tool lines or reply came with it (the reply is not stored, BACKLOG 139)
  // — so the chat says so instead of falling silent. `convo`: the run's
  // conversation. A newer one has since opened → the chat writes are its
  // predecessor's and are dropped, but the staged log still opens the sheet.
  // `photo` (§3.4b): the turn was a plate, so the chat is the surface and
  // the staging sheet does NOT open over the composer.
  const agentDone = (
    log: LogCreateResponse,
    replay: boolean,
    userId: string,
    convo: number,
    photo = false,
  ): void => {
    if (demo) refreshMe(); // the attempt counted — the banner learns it
    if (log.agent?.weight) setWeightsKey((k) => k + 1);
    const mine = inConvo(convo);
    // a correction: the earlier turn's log this one replaces
    if (mine) for (const id of log.agent?.discarded ?? []) markResult(id, "replaced");
    // §3.4b: adjust_photo edited the plate — patch its card, never add one
    if (mine && log.agent?.updated) patchResult(log.agent.updated);
    if (replay && mine) {
      // a voice bubble the replay carried no transcript for stays honest
      setChat((cur) =>
        cur.map((m) =>
          m.id === userId && m.kind === "user" && m.pending
            ? { ...m, text: "(recording sent earlier)", pending: false }
            : m,
        ),
      );
      pushChat({
        kind: "agent",
        id: crypto.randomUUID(),
        text: log.items.length
          ? "That one had already gone through — here is what it staged."
          : "That one reached the server earlier, but its answer did not make it back here — if it asked you something, say it again.",
      });
    }
    if ((log.saved_meal || log.note) && mine) {
      pushChat({
        kind: "agent",
        id: crypto.randomUUID(),
        text: log.saved_meal
          ? `Saved as "${log.saved_meal.name}" · ${log.saved_meal.item_count} item${log.saved_meal.item_count === 1 ? "" : "s"}`
          : (log.note as string),
      });
    }
    if (log.items.length === 0) {
      t0Ref.current = null; // nothing will render this log — stop its clock
      return; // the reply bubble is the answer
    }
    if (mine) {
      pushChat({ kind: "result", id: crypto.randomUUID(), log, state: "staged", photo });
    }
    setSavedNote(null);
    if (photo && mine) {
      // §3.4b: the chat is the surface for a plate — the whole point is
      // correcting it in words, and the staging sheet stacks OVER the
      // composer, so opening it here would put the keyboard out of reach.
      // Worse, dismissing it arms the 6 s undo-discard, which the next
      // utterance would flush — deleting the very log being tuned. The
      // card's Review opens the sheet, on fresh rows, when they are done.
      // Only while the conversation is still open: a plate that landed in a
      // conversation since replaced has no card to be reviewed from, and the
      // staged log is real, so it falls through to the sheet like any other.
      requestAnimationFrame(() => stagedRendered(log.log_id));
      return;
    }
    setStaging({ log, restore: null });
  };

  const submit = async (text: string): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    const convo = convoRef.current; // the conversation this turn belongs to
    const justDiscarded = flushDiscard(); // a new capture settles any still-undoable discard
    const retry = pendingRef.current?.text === text ? pendingRef.current : null;
    const pendingLog = retry ?? { id: crypto.randomUUID(), text, userId: crypto.randomUUID() };
    pendingRef.current = pendingLog;
    t0Ref.current = performance.now();
    const history = chatHistory(justDiscarded);
    closeQuestions();
    const userId = pendingLog.userId;
    if (retry) resumeTurn(userId, { kind: "user", id: userId, text });
    else pushChat({ kind: "user", id: userId, text });
    try {
      const { log, replay } = await streamAgent(
        {
          kind: "text",
          id: pendingLog.id,
          text,
          captured_at: new Date().toISOString(),
          tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
          // §13: a text log typed after a failed voice capture carries the
          // failure — capture_fallback + the client-detected capture_error.
          capture_fallback: voiceFailRef.current != null,
          capture_error: voiceFailRef.current,
          history,
          // §3.4b: while a photographed plate is staged, every turn of this
          // conversation runs on the photo tier and can edit that plate
          photo_log_id: photoLogId(justDiscarded),
        },
        (ev) => {
          if (inConvo(convo)) onAgentEvent(ev, userId);
        },
      );
      if (inConvo(convo)) {
        pendingRef.current = null;
        voiceFailRef.current = null; // reported once, on the fallback log
      }
      if ("status" in log) return false; // capture_failure — text never is
      agentDone(log, replay, userId, convo);
      return true;
    } catch (e) {
      t0Ref.current = null;
      const quota = failCapture(e, null);
      if (inConvo(convo)) {
        settleRunning();
        pushChat({
          kind: "error",
          id: crypto.randomUUID(),
          text: quota
            ? (quotaRefusal(e)?.detail ?? "Demo limit reached.")
            : "Couldn't log that — check your connection and retry.",
        });
      }
      return false;
    } finally {
      setCaptureBusy(false);
    }
  };

  // §8.1d voice path: upload the validated clip; the transcript lands in the
  // chat as soon as the server has it, then the same stream as text.
  // staged_ms measures stop-tap → rendered (§11: "capture stop → staged").
  const submitAudio = async (clip: VoiceClip): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    const convo = convoRef.current; // the conversation this turn belongs to
    const justDiscarded = flushDiscard();
    t0Ref.current = performance.now();
    // A failure from an earlier attempt rides this clip (voice retried and
    // worked) so it is counted once, here — not stamped as a "fallback" on
    // some unrelated text log an hour later.
    const priorFailure = voiceFailRef.current;
    const retry = audioRef.current?.capturedAt === clip.capturedAt ? audioRef.current : null;
    const attempt =
      retry ?? { id: crypto.randomUUID(), capturedAt: clip.capturedAt, userId: crypto.randomUUID() };
    audioRef.current = attempt;
    const history = chatHistory(justDiscarded);
    closeQuestions();
    const userId = attempt.userId;
    if (retry) resumeTurn(userId, { kind: "user", id: userId, text: "", pending: true });
    else pushChat({ kind: "user", id: userId, text: "", pending: true });
    try {
      const { log, replay } = await streamAgent(
        {
          kind: "audio",
          id: attempt.id,
          clip,
          tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
          capture_error: priorFailure,
          history,
          photo_log_id: photoLogId(justDiscarded),
        },
        (ev) => {
          if (inConvo(convo)) onAgentEvent(ev, userId);
        },
      );
      // The server answered (staged or capture_failure) — the attempt is
      // spent; only a failed upload keeps the id for a retry.
      if (inConvo(convo)) audioRef.current = null;
      if ("status" in log) {
        // capture_failure — the server-side half of §13's silent-blob class:
        // audio uploaded fine
        // but transcribed to nothing. The server recorded it on the audio
        // log; the retype that follows links to it via capture_fallback.
        t0Ref.current = null;
        if (inConvo(convo)) {
          voiceFailRef.current = "empty_transcript";
          dropPending(userId);
          pushChat({
            kind: "error",
            id: crypto.randomUUID(),
            text: "I couldn't hear anything in that — try again, or type it.",
          });
        }
        return false;
      }
      if (inConvo(convo) && voiceFailRef.current === priorFailure) voiceFailRef.current = null;
      agentDone(log, replay, userId, convo);
      return true;
    } catch (e) {
      t0Ref.current = null;
      // The demo quota refusing the upload is not a voice failure: §13's
      // counter stays clean, and the chat shows the server's sentence
      // instead of "type it instead" (typing meets the same wall).
      const quota = failCapture(e, null);
      if (inConvo(convo)) {
        // what was said stays (the transcript already landed); a bubble still
        // waiting for one goes, and no tool line is left spinning
        settleRunning();
        dropPending(userId);
        if (!quota) voiceFailRef.current = "upload_failed";
        pushChat({
          kind: "error",
          id: crypto.randomUUID(),
          text: quota
            ? (quotaRefusal(e)?.detail ?? "Demo limit reached.")
            : "Couldn't upload that recording — try again, or type it.",
        });
      }
      return false;
    } finally {
      setCaptureBusy(false);
    }
  };

  // §3.4b (2026-09-08): a plate as a chat turn. The same shape as submitAudio
  // — one client uuid per shot, keyed by the capture instant so a retry of the
  // same JPEG reuses it (§5 idempotent upsert, §8.6 re-attach) — and the same
  // stream: the server runs vision and stages the rows, the chat shows the
  // read as a tool line, and the reply invites the correction.
  // `said` (2026-09-09): words sent with the picture. They make it an agent
  // turn — the model sees the image and answers what was asked — where the
  // bare shot stays the deterministic vision parse.
  const submitPhoto = async (shot: PhotoShot, said = ""): Promise<boolean> => {
    setCaptureBusy(true);
    setCaptureError(null);
    const convo = convoRef.current; // the conversation this turn belongs to
    const justDiscarded = flushDiscard();
    // the clock starts before the round trip, per attempt (BACKLOG 2) — the
    // photo lane's staged_ms has always measured upload start → rendered
    t0Ref.current = performance.now();
    const retry =
      photoRef.current?.capturedAt === shot.capturedAt ? photoRef.current : null;
    const attempt =
      retry ?? { id: crypto.randomUUID(), capturedAt: shot.capturedAt, userId: crypto.randomUUID() };
    photoRef.current = attempt;
    const history = chatHistory(justDiscarded);
    closeQuestions();
    const userId = attempt.userId;
    const bubble: ChatMessage = {
      kind: "photo",
      id: userId,
      thumb: shot.thumb,
      text: said || undefined,
    };
    if (retry) resumeTurn(userId, bubble);
    else pushChat(bubble);
    try {
      const { log, replay } = await streamAgent(
        {
          kind: "photo",
          id: attempt.id,
          shot,
          tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
          history,
          text: said || undefined,
          // only the agent lane can act on it; the bare parse ignores it
          photo_log_id: said ? photoLogId(justDiscarded) : null,
        },
        (ev) => {
          if (inConvo(convo)) onAgentEvent(ev, userId);
        },
      );
      if (inConvo(convo)) photoRef.current = null;
      if ("status" in log) return false; // capture_failure is the voice lane's
      agentDone(log, replay, userId, convo, true);
      return true;
    } catch (e) {
      t0Ref.current = null;
      const quota = failCapture(e, null);
      if (inConvo(convo)) {
        settleRunning();
        // the thumbnail stays: it is what was sent, and it is still true
        if (quota) photoRef.current = null; // a retry today meets the same 429
        pushChat({
          kind: "error",
          id: crypto.randomUUID(),
          text: quota
            ? (quotaRefusal(e)?.detail ?? "Demo limit reached.")
            : photoErrorMessage(e),
        });
      }
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
      // An honest miss (§13: never fail silently): neither FDC Branded nor
      // Open Food Facts knows this code. Say where we looked; the scanner
      // offers search.
      setCaptureBusy(false);
      setCaptureError(
        `${code.replace(/\D/g, "")} isn't in USDA FoodData Central or Open Food Facts.`,
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
    // A reopened sheet has no other way to learn these (see removedIndexesRef);
    // each mount reports only what IT removed, so they accumulate.
    if (snap.removed.length > 0) {
      const prior = removedIndexesRef.current.get(current.log.log_id) ?? [];
      removedIndexesRef.current.set(current.log.log_id, [
        ...prior,
        ...snap.removed.filter((i) => !prior.includes(i)),
      ]);
    }
    // §3.4b: closing the sheet over the plate this conversation is still
    // tuning means "back to the chat", not "throw the plate away". A photo
    // turn never auto-opens the sheet for exactly this reason; Review is the
    // only way in, and arming the discard here would delete the log the very
    // next utterance is about to correct (and silently drop the conversation
    // off the photo tier, since photo_log_id is derived from that card).
    // Every other lane keeps the §5 discard-on-close it has always had.
    if (photoLogId() === current.log.log_id) {
      setStaging(null);
      // The sheet writes to the server as you use it — a removed row, a
      // confirm, a search pick, a tier-3 result — so the card cannot keep the
      // rows it was opened with. It is not a cosmetic copy: chatHistory()
      // sends those rows to the agent as what is on the plate, and a commit
      // built from them is refused for the ones already removed.
      //
      // The snapshot IS that list, already in hand: the sheet folds every
      // server response it makes back into `items`, and `removed` is what it
      // was hiding. Taking it synchronously — rather than re-fetching the log
      // — is what makes this correct rather than merely fresher: there is no
      // window in which the next utterance reads the pre-sheet rows, and no
      // in-flight response that can land after an adjust_photo outcome and
      // overwrite a newer list with an older one. It also keeps the local
      // portion edits, which is how the sheet has always worked — they are
      // what a commit from this card would send.
      patchResult({
        log_id: current.log.log_id,
        meal: snap.meal,
        items: snap.items.filter((i) => !snap.removed.includes(i.item_index)),
      });
      return;
    }
    flushDiscard(); // at most one undoable discard at a time
    setStaging(null);
    const entry = { log: current.log, snap };
    setDiscard(entry);
    discardTimer.current = setTimeout(() => {
      discardTimer.current = null;
      // Undo window passed: the discard lands server-side (status:'discarded'
      // — adherence-relevant data, §5), and the chat's card follows.
      discardLog(entry.log.log_id).catch(() => {});
      markResult(entry.log.log_id, "discarded");
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

  // §7.1 (2026-09-09): the staged list is the Agent lane's upper pane and
  // every other lane's own sheet, so it is built once here and placed twice —
  // `docked` is the only difference. Defined after closeStaging, which it
  // closes over.
  const stageEl = staging ? (
    <StagingSheet
      log={staging.log}
      restore={staging.restore}
      docked={lane === "agent"}
      removedIndexes={removedIndexesRef.current.get(staging.log.log_id)}
      onRendered={() => stagedRendered(staging.log.log_id)}
      onCommitted={(t, stragglers) => {
        // The commit's totals are for the day the entries landed on (§7.2
        // when-shift), which for a back-dated log is not the day this tracker
        // shows. `totals_day` says which day they describe — null when the
        // commit wrote to more than one — so a same-day commit still paints
        // instantly and only the others wait for the diary read.
        if (t.totals_day === localDateISO()) setTotals(t);
        reloadDiary();
        markResult(staging.log.log_id, "committed");
        setStaging(null);
        if (stragglers.length > 0) {
          addPending({ logId: staging.log.log_id, items: stragglers });
        }
      }}
      onClose={closeStaging}
    />
  ) : null;

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

      <WeightCard refreshKey={weightsKey} />

      {pending.map((p) => (
        <PendingItems
          key={p.logId}
          logId={p.logId}
          items={p.items}
          // The straggler is a new diary row — and the totals the append
          // answers with are for the day that ENTRY landed on, which for a
          // when-shifted log is not today, so the read is what paints.
          onTotals={() => reloadDiary()}
          onDone={() => dropPendingLog(p.logId)}
        />
      ))}

      {meals ? (
        <DiaryList
          meals={meals}
          onPatched={(entry, t) => {
            setTotals(t);
            setMeals((cur) => (cur ? placeEntry(cur, entry) : cur));
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

      {/* §7.1 (2026-09-09): one sheet, two panes — the staged rows above the
          conversation instead of over it. The chat is SplitSheet's child
          whether or not anything is staged: taking it in and out of the split
          as rows come and go would remount it, blanking the composer and
          re-running the mount effect that picks the mic or the photo picker. */}
      {lane === "agent" ? (
        <SplitSheet
          stage={stageEl}
          // Escape reaches every window listener: while a sub-sheet is stacked
          // above the split it is theirs, and while rows are staged the staged
          // list's own × is what acts (SplitSheet hides this one then).
          onDismiss={() => {
            if (!staging) setLane(null);
          }}
          chat={
            <AgentChat
              key={convoKey}
              docked
              messages={chat}
              busy={captureBusy}
              error={captureError}
              openWith={openWith}
              locate={me?.is_owner === true}
              onSubmit={submit}
              onSubmitAudio={submitAudio}
              onSubmitPhoto={submitPhoto}
              onVoiceFailure={(e) => {
                voiceFailRef.current = e;
              }}
              onReview={(log) => {
                // Inside the 6 s undo window the sheet's log is about to be
                // discarded: Review is the undo — the timer is cancelled and
                // the rows come back with their local edits, exactly as the
                // toast's Undo does. Otherwise bring the staged log back up.
                if (discardRef.current?.log.log_id === log.log_id) {
                  undoDiscard();
                  return;
                }
                if (staging?.log.log_id === log.log_id) return; // already up
                setSavedNote(null);
                setStaging({ log, restore: null });
              }}
              onClose={() => {
                if (!staging) setLane(null);
              }}
            />
          }
        />
      ) : null}

      {lane === "search" ? (
        <ManualSearchSheet
          phrase=""
          fullScreen
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

      {lane === "agent" ? null : stageEl}

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
