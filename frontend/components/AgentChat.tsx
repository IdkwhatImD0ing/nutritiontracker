"use client";

import { useEffect, useRef, useState } from "react";
import type { LogCreateResponse } from "@/lib/api";
import { ArrowUp, Camera, Check, ChevronDown, Close, Mic } from "./icons";
import PhotoCapture, { type PhotoShot } from "./PhotoCapture";
import { SheetClose } from "./SheetHeader";
import { useSheetBack } from "./sheetBack";
import useVoiceCapture, {
  type VoiceClip,
  type VoiceFailure,
} from "./useVoiceCapture";

/** One entry of the chat (§6.4, 2026-09-06). The parent (Today) owns the
 *  list — the staging sheet stacks over the chat, so the conversation
 *  outlives that; opening the lane clears it (2026-09-08: every opening
 *  is a new conversation) — and appends to it from the POST /api/agent
 *  stream: what you said (typed, or the transcript once it lands), each
 *  tool the agent called with what it answered, the agent's reply, and
 *  the staged result with its Review button. */
export type ChatMessage =
  | { kind: "user"; id: string; text: string; pending?: boolean }
  /** §3.4b: a plate sent as a turn. `thumb` is a small data: URL (the chat is
   *  persisted, so a blob: URL would be dead after a reload) and may be null
   *  when the browser could not produce one. */
  | {
      kind: "photo";
      id: string;
      thumb: string | null;
      /** said alongside it — with words it is a turn the agent can see */
      text?: string;
      pending?: boolean;
    }
  | {
      kind: "tool";
      id: string;
      name: string;
      args: Record<string, unknown>;
      output?: unknown;
      done: boolean;
    }
  /** `options`: the agent asked a clarifying question — tappable answers */
  | { kind: "agent"; id: string; text: string; options?: string[] }
  | {
      kind: "result";
      id: string;
      log: LogCreateResponse;
      /** replaced: a later turn corrected it and staged a new log */
      state: "staged" | "committed" | "discarded" | "replaced";
      /** §3.4b: this card is a photographed plate — later turns tune it in
       *  place, so it is patched rather than replaced by a second card */
      photo?: boolean;
    }
  /** `retryable`: a text or photo send that may go through on a second try
   *  (never the demo quota) — the chat offers Retry under it */
  | { kind: "error"; id: string; text: string; retryable?: boolean };

type Props = {
  messages: ChatMessage[];
  busy: boolean;
  /** the lane's error surface — the demo quota's own sentence (§13) */
  error: string | null;
  /** What the lane was opened for. "photo" opens the picker instead of
   *  starting the mic: the OS camera takes the mic anyway, and a recording
   *  started here would burn STT spend on room noise and file a voice
   *  failure the user never caused (§13's counter). */
  openWith: "mic" | "photo";
  /** Send a plate as a turn. With `text` the agent sees the picture and
   *  answers; without it the shot is the deterministic plate parse. */
  onSubmitPhoto: (shot: PhotoShot, text: string) => Promise<boolean>;
  /** Returns true on success. The field clears as the send starts (the words
   *  are in the bubble now) and a failed send puts them back — the photo too —
   *  so it can be resubmitted unchanged (same client uuid upstream). */
  onSubmit: (text: string) => Promise<boolean>;
  /** Upload a validated voice clip (§8.1d). Resolves false on any failure —
   *  upload error or the server's capture_failure — and the parent records
   *  the reason for the next text log's capture_fallback/capture_error. */
  onSubmitAudio: (clip: VoiceClip) => Promise<boolean>;
  /** Client-detected capture failure (permission, recorder, invalid blob) —
   *  parent stamps it onto the next text log (§13 telemetry). */
  onVoiceFailure: (error: VoiceFailure) => void;
  /** the result card's Review: bring the staged list back into the split */
  onReview: (log: LogCreateResponse) => void;
  /** a camera shot carries where the phone was (PhotoCapture's `locate`) */
  locate?: boolean;
  /** the raw call and result under each tool line — the owner's only
   *  (Bill, 2026-10-13); everyone else reads the sentence and its result */
  raw?: boolean;
  /** Rendered as the lower pane of <SplitSheet> rather than as a sheet of its
   *  own: no backdrop and no rounded shell — the split owns both — so the
   *  staged list stays on screen while the conversation continues under it
   *  (Bill, 2026-09-09: "I just wanna chat with the stage window open"). */
  docked?: boolean;
  /** backdrop / × / Escape / Back. A recording in progress is dropped —
   *  unmount stops the tracks (useVoiceCapture's cleanup). Docked, the split
   *  owns all of these (SplitSheet's onDismiss). */
  onClose: () => void;
};

const NOTICE: Record<VoiceFailure, string> = {
  permission_denied: "Mic permission denied — type it instead.",
  recorder_error: "Couldn't use the mic — type it instead.",
  too_short: "That was too short — hold the mic longer, or type it.",
  silent_blob: "No audio came through — type it instead.",
};

function mmss(ms: number): string {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor" />
    </svg>
  );
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

function count(v: unknown): number | null {
  return Array.isArray(v) ? v.length : null;
}

/** One adjust_photo change in words: "+ 1 banana", "− sauce", "3 eggs". */
function changeWords(c: Record<string, unknown>): string {
  const what = str(c.item);
  const amount = c.qty != null ? `${str(c.qty)}${c.unit ? ` ${str(c.unit)}` : ""} ` : "";
  if (c.op === "remove") return `− ${what}`;
  return `${c.op === "add" ? "+ " : ""}${amount}${what}`;
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** A tool's refusal in words, by its error code (backend agent.py and the
 *  deps in routes/logs.py and photo.py). The code itself stays in the
 *  owner's raw result under the line; one not listed reads as the plain
 *  failure. */
const TOOL_ERRORS: Record<string, string> = {
  already_called: "Skipped — already done this turn",
  food_in_flight: "Too early — the food was still being looked up",
  empty: "No foods were named",
  no_items: "No foods were named",
  empty_phrase: "One of the items named no food",
  invalid_qty: "An amount wasn't above zero",
  missing_grams: "A count of pieces needed its weight",
  invalid_weight: "That weight didn't look right",
  invalid_name: "No meal name was given",
  nothing_to_change: "No change was named",
  invalid_swap: "A swap didn't say what to swap in",
  choose_one: "Got foods and a log — it takes one",
  too_many: "Too many changes at once",
  unavailable: "Not available here",
  unknown_log: "That list isn't from this conversation",
  not_staged: "That list was already logged or discarded",
  not_saved: "That list was discarded — nothing to save",
  nothing_to_save: "Nothing logged yet to save",
  empty_log: "Nothing logged yet to save",
  not_found: "No saved meal by that name",
  duplicate_name: "A meal already has that name",
  unresolved: "Couldn't find those foods",
  ambiguous_item: "That matches more than one item",
  unknown_item: "No item by that name",
  repeated_item: "The same item was changed twice",
};

/** A tool line in words: what the agent is doing, then what came back (the
 *  point of the window is seeing the work). For the owner the raw call and
 *  result stay one tap away under it (Bill, 2026-10-13: owner only). A run
 *  that ended before the tool answered leaves the line settled as
 *  interrupted, never spinning. `state` colours the line: a step that
 *  errored (a weigh-in that didn't save) must not read as done. */
function describeTool(m: Extract<ChatMessage, { kind: "tool" }>): {
  doing: string;
  result: string | null;
  state: "ok" | "failed" | "interrupted";
} {
  const a = m.args;
  const out =
    m.output && typeof m.output === "object" ? (m.output as Record<string, unknown>) : null;
  // a tool that answered an error object — or that raised: the SDK then hands
  // the model a plain sentence, not JSON, and the line must not read as done
  const raised = typeof m.output === "string";
  const err = out && "error" in out ? str(out.error) : raised ? (m.output as string) : null;
  let doing: string;
  let ok: string; // the result line when the tool answered without an error
  switch (m.name) {
    case "web_search": {
      const sources = out && Array.isArray(out.sources) ? (out.sources as string[]) : [];
      // one host once, the first few — the full list is in the raw result
      const hosts = [...new Set(sources.map(host))];
      const shown = hosts.slice(0, 3).join(", ");
      const more = hosts.length > 3 ? ` +${hosts.length - 3} more` : "";
      // live: the line is up before the query is known (the completed item brings it)
      doing = a.query ? `Searching the web: “${str(a.query)}”` : "Searching the web…";
      ok = hosts.length ? `Read ${shown}${more}` : "Done";
      break;
    }
    case "read_photo": {
      // §3.4b: no model called this — the photo turn emits the pair itself so
      // the vision call is visible while it runs (it is the slowest step).
      const list = out && Array.isArray(out.seen) ? (out.seen as unknown[]) : null;
      // 2026-09-12: the nearest place the phone was at, when one was looked up
      const near = Array.isArray(a.near) && a.near.length ? str(a.near[0]) : "";
      doing = near ? `Reading the plate near ${near}…` : "Reading the plate…";
      ok =
        list == null
          ? "Done"
          : list.length === 0
            ? "No food in that photo"
            : `Saw ${list.slice(0, 4).map(str).join(", ")}` +
              (list.length > 4 ? ` +${list.length - 4} more` : "");
      break;
    }
    case "look_up": {
      // §3.4 (2026-09-11): the photo turn looks every row up before the card
      // shows — emitted by the turn itself, like read_photo
      const items = Array.isArray(a.items) ? (a.items as unknown[]).map(str) : [];
      const found = out && typeof out.found === "number" ? out.found : null;
      const total = out && typeof out.looked_up === "number" ? out.looked_up : items.length;
      doing = `Looking up ${items.slice(0, 4).map((p) => `“${p}”`).join(", ")}` +
        (items.length > 4 ? ` +${items.length - 4} more` : "");
      ok = found == null ? "Done" : `Found ${found} of ${total}`;
      break;
    }
    case "read_menu": {
      // §3.4b (2026-09-30): the café's own menu for the meal the photo falls
      // in — emitted by the photo turn itself, like read_photo
      const used = out && Array.isArray(out.matched) ? (out.matched as unknown[]).map(str) : null;
      const aside = out && Array.isArray(out.set_aside) ? (out.set_aside as unknown[]).map(str) : [];
      doing = `Checking the café's ${str(a.meal).toLowerCase() || "day"} menu…`;
      const usedText = used?.length
        ? `Using the café's numbers for ${used.slice(0, 4).join(", ")}` +
          (used.length > 4 ? ` +${used.length - 4} more` : "")
        : "";
      // matched, but the published numbers were missing or looked wrong
      const asideText = aside.length ? `Kept the database's numbers for ${aside.join(", ")}` : "";
      ok =
        used == null
          ? "Done"
          : [usedText, asideText].filter(Boolean).join(" · ") || "Nothing on the plate matched the menu";
      break;
    }
    case "adjust_photo": {
      const changes = Array.isArray(a.changes)
        ? (a.changes as Record<string, unknown>[])
        : [];
      doing = changes.length
        ? `Updating the plate: ${changes.map(changeWords).join(", ")}`
        : "Updating the plate";
      const done = out && Array.isArray(out.changed) ? (out.changed as unknown[]) : null;
      ok = done?.length ? done.map(str).join(", ") : "Updated";
      break;
    }
    case "log_food": {
      // §6.4 (2026-09-11): the agent names each food itself — items, not text
      const items = Array.isArray(a.items) ? (a.items as Record<string, unknown>[]) : [];
      const rows = out && Array.isArray(out.items) ? (out.items as Record<string, unknown>[]) : null;
      // the first few, like look_up — a long list would fill the screen
      doing =
        `Looking up ${items.slice(0, 4).map((it) => `“${str(it.phrase)}”`).join(", ") || "food"}` +
        (items.length > 4 ? ` +${items.length - 4} more` : "");
      // what the lookups found, never "staged" for a food that was not
      const found = rows ? rows.filter((r) => r.status === "resolved").length : 0;
      const pending = rows ? rows.filter((r) => r.status === "searching").length : 0;
      const missing = rows ? rows.length - found - pending : 0;
      ok =
        rows == null
          ? "Done"
          : rows.length === 0
            ? "Nothing to add"
            : `Found ${found} item${found === 1 ? "" : "s"}` +
              (pending ? ` — still looking up ${pending}` : "") +
              (missing ? ` — ${missing} not found, pick ${missing === 1 ? "it" : "them"} in the list` : "");
      break;
    }
    case "log_estimate": {
      const items = Array.isArray(a.items) ? (a.items as Record<string, unknown>[]) : [];
      const parts = items.map(
        (it) => `${str(it.qty)}${it.unit ? ` ${str(it.unit)}` : ""} ${str(it.phrase)}`,
      );
      const n = out ? count(out.items) : null;
      doing = `Estimating: ${parts.join(", ")}`;
      ok = `Estimated ${n ?? items.length} item${n === 1 ? "" : "s"} — confirm them in the list`;
      break;
    }
    case "log_weight":
      doing = `Logging weight: ${str(a.weight)} ${str(a.unit)}`;
      ok = "Saved";
      break;
    case "discard_log":
      // §6.4 corrections: the earlier list goes, the corrected one follows
      doing = "Discarding the earlier list";
      ok = out?.already ? "It was already discarded" : "Discarded";
      break;
    case "past_foods": {
      // §6.4's one read — no effect to report, so the line names what it looked at.
      // `count` comes off the tool itself: past ~16 rows _parse_output replaces
      // the payload with {summary, truncated} and the array is gone.
      const n = out ? (typeof out.count === "number" ? out.count : count(out.foods)) : null;
      doing = a.query
        ? `Checking what you've logged: “${str(a.query)}”`
        : "Checking what you've logged before";
      ok = n === 0 ? "Nothing logged like that" : `Found ${n ?? "?"} food${n === 1 ? "" : "s"}`;
      break;
    }
    case "save_meal": {
      // §8.4 through the agent: saved now (a committed log, or foods in
      // words), or stamped on a staged log and saved when it commits
      const n = out ? count(out.items) : null;
      const left = out ? (count(out.unresolved) ?? 0) : 0;
      const meal = str(out?.meal ?? a.name);
      doing = `Saving meal “${str(a.name)}”`;
      ok = out?.pending
        ? `Will save “${meal}” when you log it`
        : `Saved “${meal}” · ${n ?? "?"} item${n === 1 ? "" : "s"}` +
          (left ? ` — ${left} need${left === 1 ? "s" : ""} a first log` : "");
      break;
    }
    case "update_meal": {
      // the stored state, from the result — the args are what was asked for,
      // and a rename rides along with them (the name below is the new one)
      const meal = str(out?.meal ?? a.name);
      doing = `Updating meal “${str(a.name)}”`;
      ok =
        out?.is_active === false
          ? `Deactivated “${meal}”`
          : a.is_active === true && out?.is_active === true
            ? `Reactivated “${meal}”`
            : `Updated “${meal}”`;
      break;
    }
    default:
      // a tool this file has no words for yet: never its identifier on the
      // line (the owner's raw call under it names it)
      doing = m.done ? "Checked something" : "Checking something…";
      ok = "Done";
  }
  if (!m.done) return { doing, result: null, state: "ok" };
  if (err === "interrupted")
    return { doing, result: "Interrupted — the run ended before this finished", state: "interrupted" };
  if (raised) return { doing, result: "Didn't go through", state: "failed" };
  // already_called is a benign skip: the first call did the work
  if (err)
    return {
      doing,
      result: TOOL_ERRORS[err] ?? "Didn't go through",
      state: err === "already_called" ? "ok" : "failed",
    };
  return { doing, result: ok, state: "ok" };
}

function ToolLine({
  m,
  raw,
}: {
  m: Extract<ChatMessage, { kind: "tool" }>;
  raw: boolean;
}) {
  const { doing, result, state } = describeTool(m);
  return (
    <div className={`tool-line${m.done ? "" : " running"}${state === "ok" ? "" : ` ${state}`}`}>
      <div className="tool-head">
        {m.done ? null : <span className="spinner" aria-hidden="true" />}
        <span className="tool-text">{doing}</span>
      </div>
      {result ? <div className="tool-result">{result}</div> : null}
      {raw ? (
        <details className="disclosure">
          <summary>Raw call and result</summary>
          <pre>{JSON.stringify(m.args, null, 1)}</pre>
          {m.done && m.output !== undefined ? (
            <pre>{JSON.stringify(m.output, null, 1)}</pre>
          ) : null}
        </details>
      ) : null}
    </div>
  );
}

const kcalFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

/** The result card's line in words, for the chat's spoken status. */
function resultLine(m: Extract<ChatMessage, { kind: "result" }>): string {
  const n = m.log.items.length;
  const kcal = m.log.items.reduce((sum, it) => sum + (it.macros?.kcal ?? 0), 0);
  return (
    `${m.photo ? "From the photo: " : ""}${n} item${n === 1 ? "" : "s"} to review` +
    (kcal > 0 ? `, ${kcalFmt.format(kcal)} kcal` : "")
  );
}

function ResultCard({
  m,
  onReview,
}: {
  m: Extract<ChatMessage, { kind: "result" }>;
  onReview: (log: LogCreateResponse) => void;
}) {
  const n = m.log.items.length;
  const kcal = m.log.items.reduce((sum, it) => sum + (it.macros?.kcal ?? 0), 0);
  const estimated = m.log.items.filter((it) => it.estimated !== "none").length;
  return (
    <div className="result-card">
      <span className="result-text">
        {m.photo ? "From the photo ·\u00a0" : ""}
        {n} item{n === 1 ? "" : "s"} to review
        {kcal > 0 ? ` ·\u00a0${kcalFmt.format(kcal)}\u00a0kcal` : ""}
        {estimated ? (
          <span className="result-sub">
            {m.photo
              ? `${estimated} estimated — say what's off, or review them`
              : `${estimated} estimated — confirm or adjust before logging`}
          </span>
        ) : null}
      </span>
      {m.state === "staged" ? (
        <button
          type="button"
          aria-label={`Review ${n} item${n === 1 ? "" : "s"}`}
          onClick={() => onReview(m.log)}
        >
          Review
        </button>
      ) : (
        <span className="result-state">
          {m.state === "committed" ? (
            <>
              Logged <Check />
            </>
          ) : m.state === "replaced" ? (
            "Replaced"
          ) : (
            "Discarded"
          )}
        </span>
      )}
    </div>
  );
}

/** The + sheet's Agent lane as a chat window (§6.4, 2026-09-06 — formerly
 *  VoiceSheet's capture bar): the conversation up top, the composer at the
 *  bottom — mic, text field, send. Voice is still the default: recording
 *  starts on open (the user just chose the lane); while recording the
 *  composer is the stop button, a clock and a "Type instead" that drops the
 *  clip (nothing uploads, no STT spend) and focuses the field, so §8.1d's
 *  type-it-instead fallback (and iOS keyboard dictation) stays one tap away. */
export default function AgentChat({
  messages,
  busy,
  error,
  openWith,
  onSubmit,
  onSubmitAudio,
  onSubmitPhoto,
  onVoiceFailure,
  onReview,
  onClose,
  docked = false,
  locate = false,
  raw = false,
}: Props) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  // A picked shot waits in the composer instead of sending itself, so it can
  // carry a question ("is this the small size?"). Sent alone it is still the
  // §3.4b plate parse — the words are what make it a turn the agent sees.
  const [attached, setAttached] = useState<PhotoShot | null>(null);
  // True only for the picker this lane opened itself (+ sheet → Take photo).
  // That pick is the whole gesture — the user asked to log a plate, not to
  // start a sentence — so it sends, exactly as it did before the composer
  // learned to attach. Cleared on the first pick, so the camera button beside
  // the composer attaches even in a lane that opened this way.
  const openedLaneRef = useRef(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const paneRef = useRef<HTMLDivElement>(null);
  const { supported, recording, elapsedMs, start, stop, cancel } =
    useVoiceCapture();
  // The newest text or photo send, for a failed one's Retry (BACKLOG 176):
  // the same words, or the same shot — same capturedAt, so the same client
  // id (§8.6), never a second log or a second vision call.
  const [lastSend, setLastSend] = useState<
    | { kind: "text"; text: string }
    | { kind: "photo"; shot: PhotoShot; said: string }
    | null
  >(null);
  const sendText = (t: string) => {
    setLastSend({ kind: "text", text: t });
    return onSubmit(t);
  };
  const sendPhoto = (shot: PhotoShot, said: string) => {
    setLastSend({ kind: "photo", shot, said });
    return onSubmitPhoto(shot, said);
  };

  // §8.1d/§13: ANY voice failure degrades to typing — focus the text field
  // so the retype is one keystroke away (rAF: the input re-renders after
  // `recording` flips back).
  const fallBackToText = (msg: string) => {
    setNotice(msg);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  // §8.1d: type-it-instead is one tap away mid-recording too — drop the clip
  // (no upload, no STT spend) and hand focus to the field.
  const typeInstead = () => {
    cancel();
    setNotice(null);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const beginRecording = async () => {
    const res = await start();
    // null: the sheet closed (or typing won) while the mic prompt was up —
    // the hook released the stream; nothing to report.
    if (res === true || res === null) return;
    onVoiceFailure(res);
    fallBackToText(NOTICE[res]);
  };

  // What the lane does on open. `supported` is effect-set (false on the first
  // render), so the mic auto-start waits for it; a mic-less device simply
  // shows the text field. Opened for a photo, the picker comes up instead and
  // the mic is left alone — see the `openWith` prop.
  const startedRef = useRef(false);
  useEffect(() => {
    if (startedRef.current) return;
    if (openWith === "photo") {
      startedRef.current = true;
      openedLaneRef.current = true; // this pick IS the turn — see onPicked
      setPickerOpen(true);
      return;
    }
    if (!supported) return;
    startedRef.current = true;
    void beginRecording();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported, openWith]);

  // The picker dismissed without a pick. The lane's one send-on-pick is
  // spent, or the composer's next camera tap would send instead of attach.
  // And when the lane opened only for that picker (+ → Take photo) and
  // nothing has been said, the empty chat under it was never asked for:
  // the whole lane closes.
  const dismissPicker = () => {
    const laneOnly = openedLaneRef.current && messages.length === 0;
    openedLaneRef.current = false;
    setPickerOpen(false);
    if (laneOnly) onClose();
  };

  // Escape and Back (sheetBack). The picker is a sub-sheet stacked above the
  // chat, so its own header answers first. Then the attachment: closing would
  // destroy a picture that may not be re-takeable, and the sheet is one more
  // press away. Then the sheet — docked, that is the split's to close.
  useSheetBack(paneRef, () => {
    if (attached) {
      setAttached(null);
      return;
    }
    if (docked) return false;
    onClose();
  });

  // The parent worded the failure (a demo 429: "Demo accounts can log once a
  // day — resets at …") — drop this sheet's own "type it instead" notice so
  // it can't resurface once the error clears; typing meets the same wall.
  useEffect(() => {
    if (error) setNotice(null);
  }, [error]);

  // The newest line stays in view — a tool call landing, the reply — only
  // while the reader is at the bottom: someone scrolled up into an earlier
  // turn is left there, with a ↓ above the composer to come back. Their own
  // send always follows. A reply taller than the pane lands top-first, once,
  // so it reads from its start rather than its last line.
  const stuckRef = useRef(true);
  const lastTopRef = useRef(0);
  const [away, setAway] = useState(false);
  const replyRef = useRef<HTMLDivElement>(null); // the newest message, when a reply
  const placedRef = useRef<string | null>(null); // the reply already scrolled to
  const stick = (stuck: boolean) => {
    stuckRef.current = stuck;
    setAway(!stuck);
  };
  // Only the reader scrolling UP lets go. A line landing grows scrollHeight
  // under the last programmatic scroll and never moves scrollTop, so its
  // scroll event can't be mistaken for the reader leaving.
  const onBodyScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    const up = el.scrollTop < lastTopRef.current;
    lastTopRef.current = el.scrollTop;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 48) stick(true);
    else if (up) stick(false);
  };
  const jumpToLatest = () => {
    const el = bodyRef.current;
    if (!el) return;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top: el.scrollHeight, behavior: still ? "auto" : "smooth" });
    stick(true);
  };
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const newest = messages[messages.length - 1];
    // a fresh conversation and the user's own send always follow
    const own = !newest || newest.kind === "user" || newest.kind === "photo";
    if (!own && !stuckRef.current) return;
    const reply = replyRef.current;
    if (newest?.kind === "agent" && reply && placedRef.current !== newest.id) {
      placedRef.current = newest.id;
      if (reply.offsetHeight > el.clientHeight) {
        const top = reply.getBoundingClientRect().top - el.getBoundingClientRect().top;
        el.scrollTo({ top: el.scrollTop + top - 12 });
        stick(false);
        return;
      }
    }
    el.scrollTo({ top: el.scrollHeight });
    stick(true);
  }, [messages, busy]);

  const toggleMic = async () => {
    if (busy) return;
    setNotice(null);
    if (!recording) {
      await beginRecording();
      return;
    }
    const result = await stop();
    if (result == null) return;
    if ("error" in result) {
      onVoiceFailure(result.error);
      fallBackToText(NOTICE[result.error]);
      return;
    }
    const ok = await onSubmitAudio(result.clip);
    if (!ok) {
      // Parent already recorded the reason (upload_failed/empty_transcript).
      fallBackToText("Couldn't process that recording — type it instead.");
    }
  };

  // The conversation is not a live region: that read every echo of what was
  // just typed, "Thinking…" and each raw call aloud. What is new is said
  // here instead (§6.4: you hear what it looks up): each tool's doing line as
  // it starts and its result as it lands, the reply, the result card, and a
  // voice transcript as it replaces "Transcribing…". `said` keys what was
  // spoken, so a re-attached stream's replay stays quiet; whatever the chat
  // mounted with is history, not news.
  const [spoken, setSpoken] = useState("");
  const saidRef = useRef<Set<string> | null>(null);
  useEffect(() => {
    const quiet = saidRef.current == null;
    const said = (saidRef.current ??= new Set());
    const lines: string[] = [];
    const say = (key: string, text: string | null) => {
      if (said.has(key)) return;
      said.add(key);
      if (text && !quiet) lines.push(text);
    };
    for (const m of messages) {
      if (m.kind === "user") {
        if (m.pending) said.add(`${m.id}:pending`);
        else if (said.has(`${m.id}:pending`)) say(m.id, m.text);
      } else if (m.kind === "tool") {
        const { doing, result } = describeTool(m);
        say(`${m.id}:start`, doing);
        if (m.done) say(`${m.id}:end`, result);
      } else if (m.kind === "agent") {
        say(m.id, m.text);
      } else if (m.kind === "result") {
        say(m.id, resultLine(m));
      }
    }
    if (lines.length) setSpoken(lines.join(". "));
  }, [messages]);

  const last = messages[messages.length - 1];
  // "Thinking…" between the send and the first tool line, and between a
  // finished tool and the reply — never while a tool line already spins or
  // the voice bubble still says "Transcribing…"
  const thinking =
    busy &&
    !(last?.kind === "tool" && !last.done) &&
    !(last?.kind === "user" && last.pending);
  // The quota's sentence is already the newest bubble — once is enough.
  const errorShown = last?.kind === "error" && last.text === error;
  const canRetry = last?.kind === "error" && last.retryable === true && lastSend != null;

  const retry = async () => {
    const s = lastSend;
    if (busy || !s) return;
    if (recording) cancel(); // the retry is the turn now
    setNotice(null);
    const ok =
      s.kind === "text" ? await sendText(s.text) : await sendPhoto(s.shot, s.said);
    if (!ok) return;
    // a failed composer send is still in the composer — it went through now
    const said = s.kind === "text" ? s.text : s.said;
    setText((cur) => (cur.trim() === said ? "" : cur));
    if (s.kind === "photo") setAttached((cur) => (cur === s.shot ? null : cur));
  };

  return (
    <>
      {docked ? null : <div className="agent-backdrop" onClick={onClose} />}
      <div
        className={docked ? "split-pane chat-pane" : "sheet agent-sheet"}
        // docked, it is a pane of SplitSheet's dialog, not a second dialog
        role={docked ? "region" : "dialog"}
        aria-label={docked ? "Conversation" : "Agent"}
        ref={paneRef}
      >
        {docked ? null : (
          <div className="sheet-head">
            <div className="grabber" />
            <div className="sheet-head-row">
              <span className="sheet-title">Agent</span>
              <SheetClose onClick={onClose} />
            </div>
          </div>
        )}

        <div className="sheet-body chat-body" ref={bodyRef} onScroll={onBodyScroll}>
          {/* just above the composer, by the thumb. No tappable examples:
              sending one would log a meal or a weigh-in for real. */}
          {messages.length === 0 ? (
            <div className="chat-empty">
              <span className="chat-empty-glyph" aria-hidden="true">
                {supported ? <Mic /> : <Camera />}
              </span>
              <p className="chat-empty-title">Tell it what you ate</p>
              <p>
                {supported
                  ? "Say it, type it, or snap the plate — what you ate, your weight, a platter from the deli with no label. You'll see what the agent looks up and what it adds."
                  : "No microphone here — type what you ate, or snap the plate."}
              </p>
            </div>
          ) : null}
          {messages.map((m) => {
            switch (m.kind) {
              case "user":
                return (
                  <div
                    key={m.id}
                    className={`bubble user${m.pending ? " pending" : ""}`}
                  >
                    {m.pending ? "Transcribing…" : m.text}
                  </div>
                );
              case "photo":
                return (
                  <div key={m.id} className="bubble-wrap photo-wrap">
                    <div className="bubble user photo-bubble">
                      {m.thumb ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={m.thumb} alt="The plate you sent" />
                      ) : (
                        <>
                          <Camera /> Photo
                        </>
                      )}
                    </div>
                    {m.text ? <div className="bubble user">{m.text}</div> : null}
                  </div>
                );
              case "tool":
                return <ToolLine key={m.id} m={m} raw={raw} />;
              case "agent":
                return (
                  <div
                    key={m.id}
                    className="bubble-wrap"
                    ref={m === last ? replyRef : undefined}
                  >
                    <div className="bubble agent">{m.text}</div>
                    {m.options?.length ? (
                      <div className="chips agent-options" role="group" aria-label="Answers">
                        {m.options.map((opt) => (
                          <button
                            key={opt}
                            type="button"
                            className="chip"
                            disabled={busy}
                            onClick={() => {
                              // the tap is the answer: a recording in progress
                              // is dropped, not left running under the turn
                              if (recording) cancel();
                              void sendText(opt);
                            }}
                          >
                            {opt}
                          </button>
                        ))}
                      </div>
                    ) : null}
                  </div>
                );
              case "result":
                return <ResultCard key={m.id} m={m} onReview={onReview} />;
              case "error":
                return (
                  <div key={m.id} className="bubble-wrap">
                    <div className="bubble error" role="alert">
                      {m.text}
                    </div>
                    {m === last && canRetry ? (
                      <div className="chips agent-options">
                        <button
                          type="button"
                          className="chip"
                          disabled={busy}
                          onClick={() => void retry()}
                        >
                          Retry
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
            }
          })}
          {thinking ? (
            <div className="chat-thinking">
              <span className="spinner" aria-hidden="true" />
              Thinking…
            </div>
          ) : null}
        </div>
        <p className="sr-only" role="status">
          {spoken}
        </p>

        <div className="sheet-foot chat-foot">
          {away ? (
            <button
              type="button"
              className="chat-jump"
              aria-label="Jump to the latest message"
              onClick={jumpToLatest}
            >
              <ChevronDown />
            </button>
          ) : null}
          {/* the mic is disabled while this is up, so it never shows mid-recording */}
          {attached ? (
            <div className="chat-attached">
              {attached.thumb ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={attached.thumb} alt="Attached photo" />
              ) : (
                <Camera />
              )}
              <span className="chat-attached-hint">Photo attached</span>
              <button
                type="button"
                aria-label="Remove photo"
                onClick={() => setAttached(null)}
                disabled={busy}
              >
                <Close />
              </button>
            </div>
          ) : null}
          {recording ? (
            <div className="chat-rec">
              <button
                type="button"
                className="voice-big"
                aria-label="Stop recording"
                disabled={busy}
                onClick={toggleMic}
              >
                <StopIcon />
              </button>
              <div>
                <div className="voice-timer num">{mmss(elapsedMs)}</div>
                <div className="voice-caption">
                  <span className="rec-dot" />
                  Listening…
                </div>
              </div>
              <button type="button" className="voice-alt" onClick={typeInstead}>
                Type instead
              </button>
            </div>
          ) : (
            <form
              className="capture"
              onSubmit={async (e) => {
                e.preventDefault();
                const trimmed = text.trim();
                const shot = attached;
                if (busy || (!trimmed && !shot)) return;
                setText("");
                setAttached(null);
                const ok = shot
                  ? await sendPhoto(shot, trimmed)
                  : await sendText(trimmed);
                if (ok) {
                  setNotice(null);
                  return;
                }
                // back as it was sent (anything typed meanwhile wins): the
                // same text is the same uuid upstream, and the picture may
                // not be re-takeable
                setText((t) => t || trimmed);
                setAttached((a) => a ?? shot);
              }}
            >
              {supported ? (
                <button
                  type="button"
                  className="mic-btn"
                  aria-label={
                    attached
                      ? "Record a voice log — not while a photo is attached"
                      : "Record a voice log"
                  }
                  disabled={busy || attached !== null}
                  onClick={toggleMic}
                >
                  <Mic />
                </button>
              ) : null}
              <button
                type="button"
                className="mic-btn photo-btn"
                aria-label="Send a photo of your plate"
                disabled={busy}
                onClick={() => {
                  if (recording) cancel(); // the plate is the turn now
                  setNotice(null);
                  setPickerOpen(true);
                }}
              >
                <Camera />
              </button>
              <input
                ref={inputRef}
                type="text"
                value={text}
                onChange={(e) => {
                  setText(e.target.value);
                  setNotice(null); // typing is the fallback taken
                }}
                placeholder={attached ? "Ask about it…" : "Tell the agent…"}
                aria-label="Tell the agent"
                enterKeyHint="send"
                autoComplete="off"
              />
              <button
                type="submit"
                className="send-btn"
                disabled={busy || (!text.trim() && !attached)}
                aria-label="Send"
              >
                {busy ? <span className="spinner" /> : <ArrowUp strokeWidth={2.4} />}
              </button>
            </form>
          )}
          {error && !errorShown ? (
            <p className="capture-error" role="alert">
              {error}
            </p>
          ) : null}
          {/* kept mounted, so the notice is announced as it appears */}
          <p className="capture-notice" role="status">
            {notice && !error ? notice : null}
          </p>
        </div>
      </div>

      {/* §3.4b: the picker stacks over the chat (.subsheet is above
          .agent-sheet), decodes and downscales, and hands back the shot —
          the send, and every failure, belong to the conversation. */}
      <PhotoCapture
        open={pickerOpen}
        locate={locate}
        onClose={dismissPicker}
        onPicked={(shot) => {
          setPickerOpen(false);
          setNotice(null);
          if (openedLaneRef.current) {
            openedLaneRef.current = false;
            void sendPhoto(shot, "");
            return;
          }
          setAttached(shot);
          requestAnimationFrame(() => inputRef.current?.focus());
        }}
      />
    </>
  );
}
