"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, discardLog, type LogResponse, type Meal } from "@/lib/api";
import { createLogPhoto } from "./PhotoApi";
import "@/app/photo.css";

/** Vision needs nothing sharper than this; a 12 MP phone JPEG is ~4 MB and
 *  the route caps uploads at 12 MB, so downscale client-side, always. */
const MAX_EDGE = 1600;
const JPEG_QUALITY = 0.85;

type Props = {
  open: boolean;
  /** the Today screen's current meal, if the caller knows it */
  mealHint?: Meal;
  onClose: () => void;
  /** the upload is leaving the device (each attempt, retries included) — the
   *  caller starts its staged_ms clock here (BACKLOG 2), not when the staged
   *  log comes back */
  onUploadStart: () => void;
  /** the photo staged — the caller opens the ordinary staging sheet */
  onStaged: (log: LogResponse) => void;
};

/** One prepared shot = one client uuid (§5 idempotent upsert): a retried
 *  upload of the same JPEG reuses the id; a new pick mints a new one. */
type Shot = { id: string; blob: Blob; url: string; capturedAt: string };

type Phase = "pick" | "preparing" | "uploading" | "error";

/** Decode whatever the picker handed us. createImageBitmap honours EXIF
 *  orientation and is the only path that decodes HEIC — and only in Safari
 *  (its OS decoder; Chrome/Firefox have none). In practice iOS Safari
 *  transcodes Photos-picker HEIC to JPEG before the page ever sees it, so
 *  HEIC reaches this code from macOS Safari or a Files-app pick. The
 *  <img> fallback covers browsers without createImageBitmap options
 *  support; anything neither can decode surfaces as a plain message. */
async function decode(file: File): Promise<ImageBitmap | HTMLImageElement> {
  try {
    return await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    const url = URL.createObjectURL(file);
    try {
      const img = new Image();
      img.src = url;
      await img.decode();
      return img;
    } finally {
      URL.revokeObjectURL(url);
    }
  }
}

/** Downscale so the long edge is ≤ MAX_EDGE and re-encode as JPEG. */
async function toJpeg(src: ImageBitmap | HTMLImageElement): Promise<Blob> {
  const w = src instanceof HTMLImageElement ? src.naturalWidth : src.width;
  const h = src instanceof HTMLImageElement ? src.naturalHeight : src.height;
  if (!w || !h) throw new Error("empty image");
  const scale = Math.min(1, MAX_EDGE / Math.max(w, h));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no 2d context");
  // JPEG has no alpha — a transparent PNG/HEIC would otherwise go black.
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
  if (!(src instanceof HTMLImageElement)) src.close();
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY),
  );
  if (!blob) throw new Error("jpeg encode failed");
  return blob;
}

function uploadMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 413) return "That photo is too large to send.";
    if (e.status === 400 || e.status === 415 || e.status === 422) {
      return "That image wasn't accepted — try a JPEG or PNG.";
    }
    if (e.status === 503) {
      return "Photo recognition is unavailable right now — retry, or type it.";
    }
  }
  return "Couldn't send that photo — check your connection and retry.";
}

/** M4 (§12, §3.4a): the photo lane's entry point — a bottom sheet with
 *  "take a photo" and "choose from library". No camera stream of its own:
 *  the file input's `capture` attribute hands off to the OS camera on
 *  phones. Success hands the staged log to the caller; every failure is a
 *  visible message with a retry, never a silent dismiss. */
export default function PhotoCapture({
  open,
  mealHint,
  onClose,
  onUploadStart,
  onStaged,
}: Props) {
  const [phase, setPhase] = useState<Phase>("pick");
  const [shot, setShot] = useState<Shot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const libraryRef = useRef<HTMLInputElement>(null);

  // The preview URL lives exactly as long as its shot.
  useEffect(() => {
    if (!shot) return;
    return () => URL.revokeObjectURL(shot.url);
  }, [shot]);

  // A reopened sheet starts clean.
  useEffect(() => {
    if (open) return;
    setPhase("pick");
    setShot(null);
    setError(null);
  }, [open]);

  const upload = async (s: Shot) => {
    setPhase("uploading");
    setError(null);
    onUploadStart();
    try {
      const log = await createLogPhoto({
        id: s.id,
        file: s.blob,
        capturedAt: s.capturedAt,
        mealHint,
      });
      if (log.items.length === 0 && log.note) {
        // Vision saw no food (the route says so in `note`). Say so instead
        // of opening an empty sheet; the empty log is closed out the way an
        // abandoned sheet is (§5 status:'discarded'), and the next attempt
        // is a fresh log.
        discardLog(log.log_id).catch(() => {});
        setShot(null);
        setPhase("error");
        setError("No food found in that photo — try another angle, or type it.");
        return;
      }
      if (log.items.length === 0) {
        // An itemless replay (§8.6): an earlier attempt under this id is
        // still mid-pipeline server-side, so nothing is known yet. Keep the
        // shot — Retry asks again, and a dead attempt is re-run once the
        // grace period passes. Never discard a log another request owns.
        setPhase("error");
        setError("Still looking at that photo — give it a few seconds and retry.");
        return;
      }
      setShot(null);
      setPhase("pick");
      onStaged(log);
    } catch (e) {
      // The shot stays so Retry re-sends it under the same id.
      setPhase("error");
      setError(uploadMessage(e));
    }
  };

  const onFile = async (file: File | undefined) => {
    if (!file) return;
    setPhase("preparing");
    setError(null);
    try {
      const blob = await toJpeg(await decode(file));
      const s: Shot = {
        id: crypto.randomUUID(),
        blob,
        url: URL.createObjectURL(blob),
        capturedAt: new Date().toISOString(),
      };
      setShot(s);
      await upload(s);
    } catch {
      setShot(null);
      setPhase("error");
      setError(
        "Couldn't read that photo — this browser can't decode it. Try a JPEG or PNG, or take a new one.",
      );
    }
  };

  if (!open) return null;

  // Closing mid-upload would orphan a log the server is about to stage, so
  // the sheet is modal for those few seconds.
  const locked = phase === "preparing" || phase === "uploading";
  const close = () => {
    if (!locked) onClose();
  };

  return (
    <>
      <div className="subsheet-backdrop" onClick={close} />
      <div className="subsheet" role="dialog" aria-label="Log a photo">
        <div className="sheet-head-row">
          <span className="subsheet-title">Log a photo</span>
          <button
            type="button"
            className="sheet-close"
            aria-label="Close"
            disabled={locked}
            onClick={close}
          >
            ✕
          </button>
        </div>

        <input
          ref={cameraRef}
          type="file"
          accept="image/*"
          capture="environment"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = ""; // re-picking the same file must fire again
            void onFile(f);
          }}
        />
        <input
          ref={libraryRef}
          type="file"
          accept="image/*"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = "";
            void onFile(f);
          }}
        />

        {phase === "pick" ? (
          <>
            <p className="subsheet-hint">
              Foods are matched to the database; portions are estimated from
              the photo and flagged for you to confirm.
            </p>
            <div className="photo-options">
              <button
                type="button"
                className="photo-option"
                onClick={() => cameraRef.current?.click()}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <path d="M4 8h3l2-3h6l2 3h3a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z" />
                  <circle cx="12" cy="13" r="3.5" />
                </svg>
                Take a photo
              </button>
              <button
                type="button"
                className="photo-option"
                onClick={() => libraryRef.current?.click()}
              >
                <svg
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-hidden="true"
                >
                  <rect x="3" y="4" width="18" height="16" rx="2" />
                  <circle cx="9" cy="10" r="2" />
                  <path d="M21 16l-5-5-8 8" />
                </svg>
                Choose from library
              </button>
            </div>
          </>
        ) : null}

        {shot ? (
          <img className="photo-preview" src={shot.url} alt="Your plate" />
        ) : null}

        {phase === "preparing" ? (
          <p className="photo-status" aria-live="polite">
            <span className="spinner" />
            Preparing photo…
          </p>
        ) : null}

        {phase === "uploading" ? (
          <p className="photo-status" aria-live="polite">
            <span className="spinner" />
            Looking at your plate… this takes a few seconds.
          </p>
        ) : null}

        {phase === "error" ? (
          <>
            <p className="sheet-error">{error}</p>
            <div className="photo-actions">
              {shot ? (
                <button
                  type="button"
                  className="primary"
                  onClick={() => void upload(shot)}
                >
                  Retry
                </button>
              ) : null}
              <button
                type="button"
                onClick={() => {
                  setShot(null);
                  setError(null);
                  setPhase("pick");
                }}
              >
                {shot ? "Choose another" : "Try another photo"}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </>
  );
}
