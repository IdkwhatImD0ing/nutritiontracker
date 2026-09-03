"use client";

import { useEffect, useRef, useState } from "react";

/**
 * MediaRecorder voice capture (DESIGN.md §8.1d, §13).
 *
 * Tap-to-start / tap-to-stop; on stop the blob is validated against the iOS
 * silent-blob failure class (§13: live-looking track, empty audio — the
 * recorder "works" but produces nothing). Thresholds:
 *
 *  - MIN_CLIP_MS      500   elapsed recording time must exceed 0.5s
 *  - MIN_BLOB_BYTES   1024  absolute floor — a header-only container (the
 *                           dead-track blob: a moov/EBML header and no frames)
 *                           is a few hundred bytes regardless of duration
 *  - MIN_BYTES_PER_SEC 400  size must be plausible for the elapsed time. Real
 *                           audio never encodes below ~6 kbit/s (opus VBR
 *                           floor ≈ 750 B/s; AAC-LC on iOS ≈ 8 KB/s), so the
 *                           bar sits at half the opus floor: a quiet-but-live
 *                           mic passes (the server's empty-transcript check
 *                           owns that case), a dead track fails by ≥10× on
 *                           any clip over ~2s
 */
export const MIN_CLIP_MS = 500;
export const MIN_BLOB_BYTES = 1024;
export const MIN_BYTES_PER_SEC = 400;

/** audio/mp4 first (the only MediaRecorder container iOS Safari produces),
 *  then webm/opus for everything else (§8.1d). */
const MIME_CANDIDATES = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"];

/** Client-detected failure classes; each lands in logs.capture_error via the
 *  next text log (§4, §9 voice-capture failure counter). */
export type VoiceFailure =
  | "permission_denied"
  | "recorder_error"
  | "too_short"
  | "silent_blob";

export type VoiceClip = {
  blob: Blob;
  mimeType: string;
  durationMs: number;
  /** ISO capture-start time — §4: logs.captured_at is capture start */
  capturedAt: string;
};

export type StopResult = { clip: VoiceClip } | { error: VoiceFailure };

export default function useVoiceCapture() {
  // Effect-set so SSR and hydration both render "no mic" and the button
  // appears client-side only (no hydration mismatch).
  const [supported, setSupported] = useState(false);
  const [recording, setRecording] = useState(false);
  const [elapsedMs, setElapsedMs] = useState(0);

  const recRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const erroredRef = useRef(false);
  const startedRef = useRef(0);
  const capturedAtRef = useRef("");
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Bumped by every start(), by cancel() and on unmount. The mic prompt can
  // outlive all three, and cleanup() finds streamRef empty while start() is
  // still awaiting getUserMedia — so start() re-checks the epoch after the
  // await and, if the world moved on, stops the fresh stream itself.
  const epochRef = useRef(0);

  useEffect(() => {
    setSupported(
      typeof MediaRecorder !== "undefined" &&
        typeof navigator !== "undefined" &&
        !!navigator.mediaDevices?.getUserMedia,
    );
  }, []);

  const cleanup = () => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    recRef.current = null;
    setRecording(false);
  };

  // Unmount while recording: stop tracks so the mic indicator doesn't linger.
  // A start() still at the mic prompt is invalidated too (epochRef).
  useEffect(
    () => () => {
      epochRef.current++;
      cleanup();
    },
    [],
  );

  /** true: recording (or already was). A VoiceFailure: report it. null: the
   *  prompt resolved into a closed sheet, a cancel() or a later start() — the
   *  stream was released and there is nothing to report. */
  const start = async (): Promise<true | VoiceFailure | null> => {
    if (recRef.current) return true; // already recording
    const epoch = ++epochRef.current;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      return e instanceof DOMException &&
        (e.name === "NotAllowedError" || e.name === "SecurityError")
        ? "permission_denied"
        : "recorder_error";
    }
    if (epoch !== epochRef.current) {
      stream.getTracks().forEach((t) => t.stop());
      return null;
    }
    let rec: MediaRecorder;
    const mime = MIME_CANDIDATES.find((m) => MediaRecorder.isTypeSupported(m));
    try {
      rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch {
      stream.getTracks().forEach((t) => t.stop());
      return "recorder_error";
    }
    chunksRef.current = [];
    erroredRef.current = false;
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    rec.onerror = () => {
      erroredRef.current = true;
    };
    recRef.current = rec;
    streamRef.current = stream;
    capturedAtRef.current = new Date().toISOString();
    startedRef.current = performance.now();
    rec.start(1000); // 1s timeslices so a crash mid-clip still leaves chunks
    setElapsedMs(0);
    setRecording(true);
    timerRef.current = setInterval(
      () => setElapsedMs(performance.now() - startedRef.current),
      250,
    );
    return true;
  };

  const stop = async (): Promise<StopResult | null> => {
    const rec = recRef.current;
    if (!rec) return null;
    const durationMs = performance.now() - startedRef.current;
    const stopped = new Promise<void>((resolve) => {
      rec.onstop = () => resolve();
    });
    try {
      if (rec.state !== "inactive") rec.stop();
    } catch {
      erroredRef.current = true;
    }
    // onstop flushes the final dataavailable; the race guards a recorder that
    // dies without ever firing it (the §13 failure class is exactly this).
    await Promise.race([
      stopped,
      new Promise<void>((r) => setTimeout(r, 2_000)),
    ]);
    const mimeType = rec.mimeType || "audio/webm";
    const blob = new Blob(chunksRef.current, { type: mimeType });
    cleanup();

    if (erroredRef.current) return { error: "recorder_error" };
    if (durationMs < MIN_CLIP_MS) return { error: "too_short" };
    const minBytes = Math.max(
      MIN_BLOB_BYTES,
      (MIN_BYTES_PER_SEC * durationMs) / 1000,
    );
    if (blob.size < minBytes) return { error: "silent_blob" };
    return {
      clip: { blob, mimeType, durationMs, capturedAt: capturedAtRef.current },
    };
  };

  /** Drop the recording without producing a clip (§8.1d type-it-instead):
   *  stop the recorder and the tracks, discard the chunks, nothing uploads. */
  const cancel = () => {
    epochRef.current++; // a start() still at the mic prompt drops its stream
    const rec = recRef.current;
    if (!rec) return;
    rec.ondataavailable = null;
    try {
      if (rec.state !== "inactive") rec.stop();
    } catch {
      /* already dead — the tracks stop below regardless */
    }
    chunksRef.current = [];
    cleanup();
  };

  return { supported, recording, elapsedMs, start, stop, cancel };
}
