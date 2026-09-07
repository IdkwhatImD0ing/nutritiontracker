"use client";

import { useEffect, useRef, useState } from "react";
import type { LogCreateResponse } from "@/lib/api";
import useVoiceCapture, {
  type VoiceClip,
  type VoiceFailure,
} from "./useVoiceCapture";

/** One entry of the chat (§6.4, 2026-09-06). The parent (Today) owns the
 *  list — it outlives the sheet, so closing and reopening the lane keeps
 *  the conversation — and appends to it from the POST /api/agent stream:
 *  what you said (typed, or the transcript once it lands), each tool the
 *  agent called with what it answered, the agent's reply, and the staged
 *  result with its Review button. */
export type ChatMessage =
  | { kind: "user"; id: string; text: string; pending?: boolean }
  | {
      kind: "tool";
      id: string;
      name: string;
      args: Record<string, unknown>;
      output?: unknown;
      done: boolean;
    }
  | { kind: "agent"; id: string; text: string }
  | {
      kind: "result";
      id: string;
      log: LogCreateResponse;
      state: "staged" | "committed" | "discarded";
    }
  | { kind: "error"; id: string; text: string };

type Props = {
  messages: ChatMessage[];
  busy: boolean;
  /** the lane's error surface — the demo quota's own sentence (§13) */
  error: string | null;
  /** Returns true on success; the field only clears then, so a failed send
   *  can be resubmitted unchanged (same client uuid upstream). */
  onSubmit: (text: string) => Promise<boolean>;
  /** Upload a validated voice clip (§8.1d). Resolves false on any failure —
   *  upload error or the server's capture_failure — and the parent records
   *  the reason for the next text log's capture_fallback/capture_error. */
  onSubmitAudio: (clip: VoiceClip) => Promise<boolean>;
  /** Client-detected capture failure (permission, recorder, invalid blob) —
   *  parent stamps it onto the next text log (§13 telemetry). */
  onVoiceFailure: (error: VoiceFailure) => void;
  /** the result card's Review: reopen the staging sheet over the chat */
  onReview: (log: LogCreateResponse) => void;
  /** backdrop / × / Escape. A recording in progress is dropped — unmount
   *  stops the tracks (useVoiceCapture's cleanup). */
  onClose: () => void;
};

const NOTICE: Record<VoiceFailure, string> = {
  permission_denied: "Mic permission denied — type it instead.",
  recorder_error: "Mic capture failed — type it instead.",
  too_short: "That was too short — hold the mic longer, or type it.",
  silent_blob: "No audio came through — type it instead.",
};

function mmss(ms: number): string {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function MicIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
      <path d="M12 18v4" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="6" y="6" width="12" height="12" rx="3" fill="currentColor" />
    </svg>
  );
}

function SendIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 19V5M6 11l6-6 6 6" />
    </svg>
  );
}

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : String(v);
}

function count(v: unknown): number | null {
  return Array.isArray(v) ? v.length : null;
}

function host(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

/** A tool line in words: what the agent is doing, then what came back. The
 *  raw call and result stay one tap away under it (the point of the window
 *  is seeing the work). */
function describeTool(m: Extract<ChatMessage, { kind: "tool" }>): {
  doing: string;
  result: string | null;
} {
  const a = m.args;
  const out = (m.output ?? null) as Record<string, unknown> | null;
  const err = out && typeof out === "object" && "error" in out ? str(out.error) : null;
  switch (m.name) {
    case "web_search": {
      const sources = out && Array.isArray(out.sources) ? (out.sources as string[]) : [];
      // one host once, the first few — the full list is in the details
      const hosts = [...new Set(sources.map(host))];
      const shown = hosts.slice(0, 5).join(", ");
      const more = hosts.length > 5 ? ` +${hosts.length - 5} more` : "";
      return {
        doing: `Searching the web: “${str(a.query)}”`,
        result: !m.done ? null : hosts.length ? `Read ${shown}${more}` : "Done",
      };
    }
    case "log_food": {
      const n = out ? count(out.items) : null;
      return {
        doing: `Looking up “${str(a.text)}”`,
        result: !m.done
          ? null
          : err
            ? `Couldn't stage that (${err})`
            : n === 0
              ? "Nothing to stage"
              : `Staged ${n} item${n === 1 ? "" : "s"}`,
      };
    }
    case "log_estimate": {
      const items = Array.isArray(a.items) ? (a.items as Record<string, unknown>[]) : [];
      const parts = items.map(
        (it) => `${str(it.qty)}${it.unit ? ` ${str(it.unit)}` : ""} ${str(it.phrase)}`,
      );
      const n = out ? count(out.items) : null;
      return {
        doing: `Estimating: ${parts.join(", ")}`,
        result: !m.done
          ? null
          : err
            ? `Couldn't stage that (${err})`
            : `Staged ${n ?? items.length} estimated row${n === 1 ? "" : "s"} — confirm them in the list`,
      };
    }
    case "log_weight":
      return {
        doing: `Logging weight: ${str(a.weight)} ${str(a.unit)}`,
        result: !m.done ? null : err ? `Not saved (${err})` : "Saved",
      };
    default:
      return { doing: m.name, result: m.done ? "Done" : null };
  }
}

function ToolLine({ m }: { m: Extract<ChatMessage, { kind: "tool" }> }) {
  const { doing, result } = describeTool(m);
  return (
    <div className={`tool-line${m.done ? "" : " running"}`}>
      <div className="tool-head">
        {m.done ? null : <span className="spinner" aria-hidden="true" />}
        <span className="tool-name">{m.name}</span>
        <span className="tool-text">{doing}</span>
      </div>
      {result ? <div className="tool-result">{result}</div> : null}
      <details>
        <summary>details</summary>
        <pre>{JSON.stringify(m.args, null, 1)}</pre>
        {m.done && m.output !== undefined ? (
          <pre>{JSON.stringify(m.output, null, 1)}</pre>
        ) : null}
      </details>
    </div>
  );
}

const kcalFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });

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
        {n} item{n === 1 ? "" : "s"} staged
        {kcal > 0 ? ` · ${kcalFmt.format(kcal)} kcal` : ""}
        {estimated ? (
          <span className="result-sub">
            {estimated} estimated — confirm or adjust before committing
          </span>
        ) : null}
      </span>
      {m.state === "staged" ? (
        <button type="button" onClick={() => onReview(m.log)}>
          Review
        </button>
      ) : (
        <span className="result-state">
          {m.state === "committed" ? "Committed ✓" : "Discarded"}
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
  onSubmit,
  onSubmitAudio,
  onVoiceFailure,
  onReview,
  onClose,
}: Props) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const { supported, recording, elapsedMs, start, stop, cancel } =
    useVoiceCapture();

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

  // `supported` is effect-set (false on the first render), so the auto-start
  // waits for it; a mic-less device simply shows the text field.
  const startedRef = useRef(false);
  useEffect(() => {
    if (!supported || startedRef.current) return;
    startedRef.current = true;
    void beginRecording();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [supported]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The parent worded the failure (a demo 429: "Demo accounts can log once a
  // day — resets at …") — drop this sheet's own "type it instead" notice so
  // it can't resurface once the error clears; typing meets the same wall.
  useEffect(() => {
    if (error) setNotice(null);
  }, [error]);

  // the newest line is always in view — a tool call landing, the reply
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight });
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

  const last = messages[messages.length - 1];
  // "Thinking…" between the send and the first tool line, and between a
  // finished tool and the reply — never while a tool line already spins
  const thinking = busy && !(last?.kind === "tool" && !last.done);

  return (
    <>
      <div className="agent-backdrop" onClick={onClose} />
      <div className="sheet agent-sheet" role="dialog" aria-label="Agent">
        <div className="sheet-head">
          <div className="grabber" />
          <div className="sheet-head-row">
            <span className="sheet-title">Agent</span>
            <button
              type="button"
              className="sheet-close"
              aria-label="Close"
              onClick={onClose}
            >
              ✕
            </button>
          </div>
        </div>

        <div className="sheet-body chat-body" ref={bodyRef} aria-live="polite">
          {messages.length === 0 ? (
            <p className="chat-empty">
              {supported
                ? "Say or type it — what you ate, your weight, a platter from the deli with no label. You'll see what the agent looks up and what it stages."
                : "No microphone here — type what you ate, or your weight."}
            </p>
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
              case "tool":
                return <ToolLine key={m.id} m={m} />;
              case "agent":
                return (
                  <div key={m.id} className="bubble agent">
                    {m.text}
                  </div>
                );
              case "result":
                return <ResultCard key={m.id} m={m} onReview={onReview} />;
              case "error":
                return (
                  <div key={m.id} className="bubble error" role="alert">
                    {m.text}
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

        <div className="sheet-foot chat-foot">
          {recording ? (
            <div className="chat-rec" aria-live="polite">
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
                  Listening — tap to stop
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
                if (!trimmed || busy) return;
                const ok = await onSubmit(trimmed);
                if (ok) {
                  setText("");
                  setNotice(null);
                }
              }}
            >
              {supported ? (
                <button
                  type="button"
                  className="mic-btn"
                  aria-label="Record a voice log"
                  disabled={busy}
                  onClick={toggleMic}
                >
                  <MicIcon />
                </button>
              ) : null}
              <input
                ref={inputRef}
                type="text"
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder="Tell the agent…"
                aria-label="Tell the agent"
                enterKeyHint="send"
                autoComplete="off"
              />
              <button
                type="submit"
                className="send-btn"
                disabled={busy || !text.trim()}
                aria-label="Send"
              >
                {busy ? <span className="spinner" /> : <SendIcon />}
              </button>
            </form>
          )}
          {error ? <p className="capture-error">{error}</p> : null}
          {notice && !error ? <p className="capture-notice">{notice}</p> : null}
        </div>
      </div>
    </>
  );
}
