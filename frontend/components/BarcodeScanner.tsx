"use client";

import { useEffect, useRef, useState } from "react";

type Props = {
  /** true while the parent's lookup / from-food round trip is in flight —
   *  the detection loop pauses so one read can't fire twice */
  busy: boolean;
  /** the parent's outcome for the last code (not found, lookup failed); the
   *  camera stays live so the user can try again, and search is offered */
  notice: string | null;
  onCode: (code: string) => void;
  /** absent when search would meet the same wall (a demo account's day's
   *  log is spent — the notice is the server's 429) — no chip is offered */
  onSearchInstead?: () => void;
  onClose: () => void;
};

type Camera = "starting" | "live" | "denied" | "unavailable";

/** Retail formats only: the lane's job is a packaged food's GTIN for the §3.4
 *  gtinUpc ladder (sources/fdc.lookup_barcode — FDC Branded is the only
 *  barcode source; there is no Open Food Facts adapter). QR and the rest are
 *  noise here. */
const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"] as const;
const TICK_MS = 150;
/** ~1s of empty frames re-arms the last code (the user pointed away and back) */
const REARM_TICKS = 7;
const MIN_DIGITS = 6; // UPC-E prints as six

const CAMERA_TEXT: Record<Camera, string> = {
  starting: "Starting camera…",
  live: "Point the camera at the barcode.",
  denied: "Camera permission denied — type the number instead.",
  unavailable: "No camera available — type the number instead.",
};

/** The + sheet's Scan barcode lane. iOS Safari ships no BarcodeDetector, so
 *  the spec-shaped ponyfill from `barcode-detector` (ZXing-C++ wasm) reads on
 *  every browser — imported lazily so its glue stays out of the Today bundle
 *  (the ~1 MB reader wasm is fetched on the first scan, from jsDelivr by
 *  default). Camera via getUserMedia, rear-facing, tracks stopped on close;
 *  a torch toggle where the track advertises one; a typed-number fallback
 *  when the camera is denied or the code won't read. */
export default function BarcodeScanner({
  busy,
  notice,
  onCode,
  onSearchInstead,
  onClose,
}: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [camera, setCamera] = useState<Camera>("starting");
  const [attempt, setAttempt] = useState(0); // bump → re-request the camera
  const [torch, setTorch] = useState<boolean | null>(null); // null = none
  const [manual, setManual] = useState("");
  // Read by the detection loop without re-subscribing it.
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const onCodeRef = useRef(onCode);
  onCodeRef.current = onCode;

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    const video = videoRef.current;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    setCamera("starting");
    setTorch(null);
    (async () => {
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: { ideal: "environment" } },
          audio: false,
        });
      } catch (e) {
        if (cancelled) return;
        setCamera(
          e instanceof DOMException &&
            (e.name === "NotAllowedError" || e.name === "SecurityError")
            ? "denied"
            : "unavailable",
        );
        requestAnimationFrame(() => inputRef.current?.focus());
        return;
      }
      if (cancelled || !video) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      streamRef.current = stream;
      video.srcObject = stream;
      await video.play().catch(() => {
        /* muted inline autoplay is allowed; a refusal just means no frames */
      });
      const caps = stream.getVideoTracks()[0]?.getCapabilities?.() as
        | (MediaTrackCapabilities & { torch?: boolean })
        | undefined;
      if (caps?.torch) setTorch(false);
      const { BarcodeDetector } = await import("barcode-detector/ponyfill");
      if (cancelled) return;
      const detector = new BarcodeDetector({ formats: [...FORMATS] });
      setCamera("live");
      let last: string | null = null;
      let misses = 0;
      const tick = async () => {
        if (cancelled) return;
        // readyState ≥ HAVE_CURRENT_DATA: detect() throws on a frameless video
        if (!busyRef.current && video.readyState >= 2) {
          let code: string | undefined;
          try {
            code = (await detector.detect(video))[0]?.rawValue;
          } catch {
            /* an undecodable frame — the next one may do */
          }
          if (code && code !== last) {
            last = code;
            misses = 0;
            if (!cancelled) onCodeRef.current(code);
          } else if (!code && ++misses >= REARM_TICKS) {
            last = null;
          }
        }
        if (!cancelled) timer = setTimeout(tick, TICK_MS);
      };
      void tick();
    })();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      if (video) video.srcObject = null;
    };
  }, [attempt]);

  const toggleTorch = async () => {
    const track = streamRef.current?.getVideoTracks()[0];
    if (!track || torch == null) return;
    try {
      await track.applyConstraints({
        advanced: [{ torch: !torch } as MediaTrackConstraintSet],
      });
      setTorch(!torch);
    } catch {
      setTorch(null); // advertised but refused — drop the toggle
    }
  };

  const digits = manual.replace(/\D/g, "");
  const hideView = camera === "denied" || camera === "unavailable";

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet" role="dialog" aria-label="Scan barcode">
        <div className="sheet-head-row">
          <div className="subsheet-title">Scan barcode</div>
          <button
            type="button"
            className="sheet-close"
            aria-label="Close"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        <div className="scan-view" hidden={hideView}>
          <video ref={videoRef} playsInline muted autoPlay />
          <div className="scan-frame" aria-hidden="true" />
          {torch != null ? (
            <button
              type="button"
              className={`scan-torch${torch ? " on" : ""}`}
              aria-pressed={torch}
              onClick={toggleTorch}
            >
              {torch ? "Torch off" : "Torch on"}
            </button>
          ) : null}
        </div>

        <p className="scan-status" aria-live="polite">
          {busy ? "Looking it up…" : CAMERA_TEXT[camera]}
        </p>
        {hideView ? (
          <div className="chips">
            <button
              type="button"
              className="chip"
              onClick={() => setAttempt((n) => n + 1)}
            >
              Try the camera again
            </button>
          </div>
        ) : null}

        {notice ? (
          <>
            <p className="sheet-error">{notice}</p>
            {onSearchInstead ? (
              <div className="chips">
                <button
                  type="button"
                  className="chip search-instead"
                  onClick={onSearchInstead}
                >
                  Search instead
                </button>
              </div>
            ) : null}
          </>
        ) : null}

        <form
          className="capture"
          onSubmit={(e) => {
            e.preventDefault();
            if (busy || digits.length < MIN_DIGITS) return;
            onCode(digits);
          }}
        >
          <input
            ref={inputRef}
            value={manual}
            onChange={(e) => setManual(e.target.value)}
            inputMode="numeric"
            placeholder="Or type the number under the bars"
            aria-label="Barcode number"
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={busy || digits.length < MIN_DIGITS}
            aria-label="Look up"
          >
            {busy ? <span className="spinner" /> : "→"}
          </button>
        </form>
      </div>
    </>
  );
}
