"use client";

import { useEffect, useRef, useState } from "react";
import { loadDetector } from "./barcodeDetector";
import { ArrowRight } from "./icons";
import SheetHeader from "./SheetHeader";

type Props = {
  /** true while the parent's lookup / from-food round trip is in flight —
   *  the detection loop pauses so one read can't fire twice, and a loading
   *  cover hides the camera so the read is visibly taken */
  busy: boolean;
  /** the parent's failure for the last code (lookup failed, a demo 429);
   *  the camera stays live so the user can try again, and search is offered */
  notice: string | null;
  /** the last code's digits when neither database knows it — an ordinary
   *  miss, said plainly (where we looked) with search as the way on */
  miss: string | null;
  onCode: (code: string) => void;
  /** absent when search would meet the same wall (a demo account's day's
   *  log is spent — the notice is the server's 429) — no search is offered */
  onSearchInstead?: () => void;
  onClose: () => void;
};

type Camera = "starting" | "live" | "denied" | "unavailable";

/** Retail formats only: the lane's job is a packaged food's GTIN for the §3.4
 *  gtinUpc ladder (FDC Branded, then Open Food Facts on a miss —
 *  backend/routes/quick.py). QR and the rest are noise here. */
const FORMATS = ["ean_13", "ean_8", "upc_a", "upc_e"] as const;
/** ~1s of detection attempts without the code re-arms it (the user pointed
 *  away and back). Attempts, not wall-clock: a lookup's own duration, when no
 *  detection runs, must not count. */
const REARM_MS = 1000;
const MIN_DIGITS = 6; // UPC-E prints as six

const CAMERA_TEXT: Record<Camera, string> = {
  starting: "Starting camera…",
  live: "Point the camera at the barcode.",
  denied: "Camera access is off. Turn it on in Settings, or type the number.",
  // also a camera that is busy elsewhere or refused the constraints, which
  // "Try the camera again" can fix
  unavailable: "Couldn't start the camera — type the number instead.",
};

/** The + sheet's Scan barcode lane. iOS Safari ships no BarcodeDetector, so
 *  the spec-shaped ponyfill from `barcode-detector` (ZXing-C++ wasm) reads on
 *  every browser — imported lazily so its glue stays out of the Today bundle
 *  (the ~1 MB reader wasm comes from jsDelivr by default, prefetched when
 *  the + sheet opens — `loadDetector`). Camera via getUserMedia, rear-facing,
 *  720p requested, tracks stopped on close;
 *  a torch toggle where the track advertises one; a typed-number fallback
 *  when the camera is denied or the code won't read. */
export default function BarcodeScanner({
  busy,
  notice,
  miss,
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
    let frame = 0;
    setCamera("starting");
    setTorch(null);
    // The decoder loads while the permission prompt is up, not after it. A
    // failure surfaces where it is awaited below, not as a stray rejection
    // while the prompt is still up (or after a denial, which never awaits).
    const detectorReady = loadDetector();
    detectorReady.catch(() => {});
    (async () => {
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: {
            facingMode: { ideal: "environment" },
            // 720p, not the 640×480 many phones hand out by default: a retail
            // code needs its bars a few pixels wide to read at arm's length
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
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
      const { BarcodeDetector } = await detectorReady;
      if (cancelled) return;
      const detector = new BarcodeDetector({ formats: [...FORMATS] });
      setCamera("live");
      let last: string | null = null;
      let missingSince: number | null = null; // first attempt that lost the code
      // Back to back on animation frames — the next detect() starts as soon
      // as the previous one settles (rAF yields to paint and idles in a
      // hidden tab), so a code is read on the first frame that shows it
      // rather than on a fixed 150 ms tick. A code stays armed while it is in
      // frame; ~1 s of attempts without it re-arms (pointed away and back).
      // The clock starts on the first empty attempt, never during a lookup
      // (no attempts run while busy), so a "not found" that took a second
      // cannot re-submit the code still in frame on the first dropped frame.
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
          if (code) {
            missingSince = null;
            if (code !== last) {
              last = code;
              if (!cancelled) onCodeRef.current(code);
            }
          } else if (last) {
            missingSince ??= performance.now();
            if (performance.now() - missingSince > REARM_MS) {
              last = null;
              missingSince = null;
            }
          }
        }
        if (!cancelled) frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    })();
    return () => {
      cancelled = true;
      cancelAnimationFrame(frame);
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
        <SheetHeader title="Scan barcode" onClose={onClose} />

        <div className={`scan-view${busy ? " busy" : ""}`} hidden={hideView}>
          <video ref={videoRef} playsInline muted autoPlay />
          <div className="scan-frame" aria-hidden="true" />
          {torch != null ? (
            <button
              type="button"
              className={`scan-torch${torch ? " on" : ""}`}
              aria-label="Torch"
              aria-pressed={torch}
              onClick={toggleTorch}
            >
              {/* a flashlight */}
              <svg
                viewBox="0 0 24 24"
                width="20"
                height="20"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M6 2h12v4l-2 4v10a2 2 0 0 1-2 2h-4a2 2 0 0 1-2-2V10L6 6z" />
                <path d="M6 6h12M12 13v2" />
              </svg>
            </button>
          ) : null}
          {/* a typed number takes this path too, so it says what is
              happening rather than what was seen; the status line below
              carries the words for a screen reader */}
          {busy ? (
            <div className="scan-loading" aria-hidden="true">
              <span className="spinner" />
              Looking it up…
            </div>
          ) : null}
        </div>

        {/* one "Looking it up…" on screen: the cover says it while the camera
            shows, this line when there is no camera view */}
        <p className="scan-status" aria-live="polite">
          {busy && !hideView ? (
            <>
              {"\u00a0"}
              <span className="sr-only">Looking it up…</span>
            </>
          ) : busy ? (
            "Looking it up…"
          ) : (
            CAMERA_TEXT[camera]
          )}
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

        {miss ? (
          <div className="scan-miss">
            <p className="scan-miss-title">No match for this barcode</p>
            <p className="subsheet-hint">
              Not in USDA FoodData Central or Open Food Facts ·{" "}
              <span className="num">{miss}</span>
            </p>
            {onSearchInstead ? (
              <button
                type="button"
                className="edit-manual-btn"
                onClick={onSearchInstead}
              >
                Search instead
              </button>
            ) : null}
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
            placeholder={hideView ? "Type the number" : "Or type the number"}
            aria-label="Barcode number"
            autoComplete="off"
          />
          <button
            type="submit"
            disabled={busy || digits.length < MIN_DIGITS}
            aria-label="Look up"
          >
            <ArrowRight strokeWidth={2.4} />
          </button>
        </form>
      </div>
    </>
  );
}
