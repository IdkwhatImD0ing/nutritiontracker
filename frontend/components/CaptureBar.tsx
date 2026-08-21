"use client";

import { useRef, useState } from "react";
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

export default function CaptureBar({
  busy,
  error,
  onSubmit,
  onSubmitAudio,
  onVoiceFailure,
}: Props) {
  const [text, setText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const { supported, recording, elapsedMs, start, stop } = useVoiceCapture();

  // §8.1d/§13: ANY voice failure degrades to typing — focus the text field
  // so the retype is one keystroke away (rAF: the input re-renders after
  // `recording` flips back).
  const fallBackToText = (msg: string) => {
    setNotice(msg);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const toggleMic = async () => {
    if (busy) return;
    setNotice(null);
    if (!recording) {
      const res = await start();
      if (res !== true) {
        onVoiceFailure(res);
        fallBackToText(NOTICE[res]);
      }
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
    <div>
      <form
        className="capture"
        onSubmit={async (e) => {
          e.preventDefault();
          const trimmed = text.trim();
          if (!trimmed || busy || recording) return;
          const ok = await onSubmit(trimmed);
          if (ok) {
            setText("");
            setNotice(null);
          }
        }}
      >
        {recording ? (
          <div className="rec-status num" aria-live="polite">
            <span className="rec-dot" />
            {mmss(elapsedMs)}
            <span className="rec-hint">listening — tap to stop</span>
          </div>
        ) : (
          <input
            ref={inputRef}
            type="text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="What did you eat?"
            aria-label="Log food"
            enterKeyHint="send"
            autoComplete="off"
          />
        )}
        {supported ? (
          <button
            type="button"
            className={`mic-btn${recording ? " rec" : ""}`}
            aria-label={recording ? "Stop recording" : "Record a voice log"}
            disabled={busy}
            onClick={toggleMic}
          >
            {recording ? (
              <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" />
              </svg>
            ) : (
              <svg
                width="18"
                height="18"
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
            )}
          </button>
        ) : null}
        <button
          type="submit"
          disabled={busy || recording || !text.trim()}
          aria-label="Log"
        >
          {busy ? <span className="spinner" /> : "↑"}
        </button>
      </form>
      {error ? <p className="capture-error">{error}</p> : null}
      {notice && !error ? <p className="capture-notice">{notice}</p> : null}
    </div>
  );
}
