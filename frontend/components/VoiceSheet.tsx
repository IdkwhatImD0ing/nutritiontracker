"use client";

import { useEffect, useRef, useState } from "react";
import useVoiceCapture, {
  type VoiceClip,
  type VoiceFailure,
} from "./useVoiceCapture";

type Props = {
  busy: boolean;
  error: string | null;
  /** Returns true on success; the field only clears then, so a failed log
   *  can be resubmitted unchanged (same client uuid upstream). */
  onSubmit: (text: string) => Promise<boolean>;
  /** Upload a validated voice clip (§8.1d). Resolves false on any failure —
   *  upload error or the server's capture_failure — and the parent records
   *  the reason for the next text log's capture_fallback/capture_error. */
  onSubmitAudio: (clip: VoiceClip) => Promise<boolean>;
  /** Client-detected capture failure (permission, recorder, invalid blob) —
   *  parent stamps it onto the next text log (§13 telemetry). */
  onVoiceFailure: (error: VoiceFailure) => void;
  /** backdrop / × / Escape. A recording in progress is dropped — unmount
   *  stops the tracks (useVoiceCapture's cleanup), and a start() still at
   *  the mic prompt releases its stream when the prompt resolves. */
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

/** The + sheet's Agent lane (§6.4, 2026-09-06 — formerly "Voice log"): the
 *  old mid-screen capture bar (mic + text field) in a sheet (owner ruling,
 *  2026-09-02), now the front door of the agent — what you say or type here
 *  can be what you ate, your weight, or both; the parent sends it with
 *  agent=true. Voice is still the default: recording starts on open (the
 *  user just chose the lane, so a second mic tap would be ceremony). While
 *  recording, the sheet is one big stop target and a clock; a "Type instead"
 *  tap cancels the recording (nothing uploads) and shows the text field, so
 *  §8.1d's type-it-instead fallback (and iOS keyboard dictation) stays one
 *  tap away even mid-recording. */
export default function VoiceSheet({
  busy,
  error,
  onSubmit,
  onSubmitAudio,
  onVoiceFailure,
  onClose,
}: Props) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
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

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Agent">
        <div className="sheet-head-row">
          <div className="subsheet-title">Agent</div>
          <button
            type="button"
            className="sheet-close"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        {recording ? (
          <div className="voice-stage" aria-live="polite">
            <button
              type="button"
              className="voice-big"
              aria-label="Stop recording"
              disabled={busy}
              onClick={toggleMic}
            >
              <StopIcon />
            </button>
            <div className="voice-timer num">{mmss(elapsedMs)}</div>
            <div className="voice-caption">
              <span className="rec-dot" />
              Listening — tap to stop
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
              placeholder="What you ate, or your weight"
              aria-label="Tell the agent"
              enterKeyHint="send"
              autoComplete="off"
            />
            <button
              type="submit"
              className="send-btn"
              disabled={busy || !text.trim()}
              aria-label="Log"
            >
              {busy ? <span className="spinner" /> : <SendIcon />}
            </button>
          </form>
        )}
        {error ? <p className="capture-error">{error}</p> : null}
        {notice && !error ? <p className="capture-notice">{notice}</p> : null}
        {!recording && !notice && !error ? (
          <p className="subsheet-hint">
            {supported
              ? "Tap the mic and say it — what you ate, or your weight — or type it."
              : "No microphone here — type what you ate, or your weight."}
          </p>
        ) : null}
      </div>
    </>
  );
}
