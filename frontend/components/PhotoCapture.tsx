"use client";

import { useEffect, useRef, useState } from "react";
import "@/app/photo.css";
import SheetHeader from "./SheetHeader";
import { SheetFrame, usePresence } from "./useSheetPresence";

/** Vision needs nothing sharper than this; a 12 MP phone JPEG is ~4 MB and
 *  the route caps uploads at 12 MB, so downscale client-side, always. */
const MAX_EDGE = 1600;
const JPEG_QUALITY = 0.85;
/** The chat bubble's thumbnail. It is persisted with the conversation (Today
 *  writes the whole chat to localStorage), so it must be a data: URL — a
 *  blob: URL is dead after a reload — and it must be small. */
const THUMB_EDGE = 240;
const THUMB_QUALITY = 0.6;
/** A camera shot is a file made moments ago. Desktop browsers ignore
 *  `capture` and open a file picker, where "Take a photo" can hand back a
 *  photo from any day — so the phone's location rides only a file this new. */
const FRESH_MS = 5 * 60_000;

/** Where the phone was (§3.4b, 2026-09-12): the Geolocation API's fix,
 *  accuracy in meters. The server turns it into the names of the places
 *  around it for the photo reader, and stores none of it. */
export type Fix = { lat: number; lng: number; accuracy: number };

/** One prepared shot (§3.4b in the chat, 2026-09-08). `capturedAt` is the
 *  retry key, exactly as VoiceClip's is: re-sending the same shot reuses the
 *  client uuid Today minted for it (§5 idempotent upsert, §8.6 re-attach). */
export type PhotoShot = {
  blob: Blob;
  mimeType: string;
  capturedAt: string;
  /** a small data: URL for the chat bubble — never the full JPEG */
  thumb: string | null;
  /** camera shots only, of a file made in the last few minutes — a library
   *  pick may have been taken anywhere, and the phone's location now would
   *  mislead the reader. Null when there was no fix in time, or the person
   *  said no. */
  where?: Fix | null;
};

type Props = {
  open: boolean;
  onClose: () => void;
  /** a decoded, downscaled shot — the caller sends it as the chat turn */
  onPicked: (shot: PhotoShot) => void;
  /** ask where the phone is while the sheet is open. The owner's only: the
   *  server looks places up for no one else (§8.9), so a demo account is
   *  never shown a prompt that could not help it. */
  locate?: boolean;
};

type Phase = "pick" | "preparing" | "error";

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

/** Draw `src` onto a canvas whose long edge is ≤ `edge`. */
function fit(src: ImageBitmap | HTMLImageElement, edge: number): HTMLCanvasElement {
  const w = src instanceof HTMLImageElement ? src.naturalWidth : src.width;
  const h = src instanceof HTMLImageElement ? src.naturalHeight : src.height;
  if (!w || !h) throw new Error("empty image");
  const scale = Math.min(1, edge / Math.max(w, h));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(w * scale);
  canvas.height = Math.round(h * scale);
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("no 2d context");
  // JPEG has no alpha — a transparent PNG/HEIC would otherwise go black.
  ctx.fillStyle = "#fff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(src, 0, 0, canvas.width, canvas.height);
  return canvas;
}

/** Downscale so the long edge is ≤ MAX_EDGE and re-encode as JPEG. */
async function toJpeg(src: ImageBitmap | HTMLImageElement): Promise<Blob> {
  const canvas = fit(src, MAX_EDGE);
  const blob = await new Promise<Blob | null>((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY),
  );
  if (!blob) throw new Error("jpeg encode failed");
  return blob;
}

/** The photo picker (§3.4b; a chat turn since 2026-09-08) — a bottom sheet
 *  with "take a photo" and "choose from library". No camera stream of its
 *  own: the file input's `capture` attribute hands off to the OS camera on
 *  phones.
 *
 *  It picks, decodes and downscales; it no longer uploads. The shot goes to
 *  the caller, which sends it as a turn of the Agent chat and shows the
 *  answer — and every failure — there, next to the conversation that is going
 *  to correct it. Two buttons a human taps is also the only shape that works:
 *  a file input cannot be opened from an effect, because the user activation
 *  is long gone by the time the lane has mounted. */
export default function PhotoCapture({ open, onClose, onPicked, locate = false }: Props) {
  const [phase, setPhase] = useState<Phase>("pick");
  const [error, setError] = useState<string | null>(null);
  const cameraRef = useRef<HTMLInputElement>(null);
  const libraryRef = useRef<HTMLInputElement>(null);
  const whereRef = useRef<Fix | null>(null);
  const { mounted, closing } = usePresence(open);

  // A reopened sheet starts clean.
  useEffect(() => {
    if (open) return;
    setPhase("pick");
    setError(null);
  }, [open]);

  // Where the phone is, asked when the sheet opens rather than on the camera
  // tap: the permission prompt then comes before the camera instead of over
  // it, and the fix has the whole shot to arrive. A denial, a timeout and a
  // fix still pending all mean the same thing — the photo goes without one.
  useEffect(() => {
    whereRef.current = null;
    if (!open || !locate || !navigator.geolocation) return;
    let live = true;
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => {
        if (live) {
          whereRef.current = {
            lat: coords.latitude,
            lng: coords.longitude,
            accuracy: coords.accuracy,
          };
        }
      },
      () => {},
      { enableHighAccuracy: true, maximumAge: 60_000, timeout: 20_000 },
    );
    return () => {
      live = false;
    };
  }, [open, locate]);

  const onFile = async (file: File | undefined, camera: boolean) => {
    if (!file) return;
    setPhase("preparing");
    setError(null);
    try {
      const src = await decode(file);
      const blob = await toJpeg(src);
      // the bubble's thumbnail, from the same decode — a data: URL, because
      // the chat is persisted and a blob: URL would not survive a reload
      let thumb: string | null = null;
      try {
        thumb = fit(src, THUMB_EDGE).toDataURL("image/jpeg", THUMB_QUALITY);
      } catch {
        /* a tainted or oversized canvas — the bubble renders without it */
      }
      if (!(src instanceof HTMLImageElement)) src.close();
      onPicked({
        blob,
        mimeType: blob.type || "image/jpeg",
        capturedAt: new Date().toISOString(),
        thumb,
        where: camera && Date.now() - file.lastModified < FRESH_MS ? whereRef.current : null,
      });
      setPhase("pick");
    } catch {
      setPhase("error");
      setError(
        "Couldn't read that photo — this browser can't decode it. Try a JPEG or PNG, or take a new one.",
      );
    }
  };

  if (!mounted) return null;

  const locked = phase === "preparing";
  const close = () => {
    if (!locked) onClose();
  };

  return (
    <SheetFrame closing={closing}>
      <div className="subsheet-backdrop" onClick={close} />
      <div className="subsheet" role="dialog" aria-label="Log a photo">
        <SheetHeader title="Log a photo" onClose={close} canClose={!locked} />

        <input
          ref={cameraRef}
          type="file"
          accept="image/*"
          capture="environment"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            e.target.value = ""; // re-picking the same file must fire again
            void onFile(f, true);
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
            void onFile(f, false);
          }}
        />

        {phase === "pick" ? (
          <>
            <p className="subsheet-hint">
              Foods are matched to the database and the amounts are estimated
              from the photo — then you tell the agent what it got wrong.
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

        {phase === "preparing" ? (
          <p className="photo-status" aria-live="polite">
            <span className="spinner" />
            Preparing photo…
          </p>
        ) : null}

        {phase === "error" ? (
          <>
            <p className="sheet-error">{error}</p>
            <div className="photo-actions">
              <button
                type="button"
                onClick={() => {
                  setError(null);
                  setPhase("pick");
                }}
              >
                Try another photo
              </button>
            </div>
          </>
        ) : null}
      </div>
    </SheetFrame>
  );
}
