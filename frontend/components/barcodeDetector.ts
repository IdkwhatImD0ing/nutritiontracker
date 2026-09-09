"use client";

/**
 * The barcode decoder's loader, on its own so that wanting to *warm* it does
 * not mean importing the scanner component.
 *
 * It lived in BarcodeScanner.tsx, and AddSheet imported that whole component
 * to reach this one function. Splitting it out is a dependency-hygiene fix,
 * not a bundle-size one: measured, moving it (and lazy-loading the scanner)
 * moved ~10 KB, because the ponyfill's glue is small and the ~1 MB reader wasm
 * is fetched from the CDN at runtime, never bundled. The lazy-loading was
 * reverted; this split stayed, because the + sheet has no business pulling in
 * a camera UI to prefetch a decoder.
 */

export type DetectorModule = typeof import("barcode-detector/ponyfill");

let detectorModule: Promise<DetectorModule> | null = null;
let wasm: Promise<unknown> | null = null;

/** The wasm fetch + instantiation, once. zxing-wasm caches the instantiation
 *  promise per overrides — a *rejected* one too (offline, a blocked CDN), and
 *  detect() would then fail until a reload, which a PWA may not see for
 *  days. So a failure is forgotten here and the retry passes `equalityFn`
 *  to force a fresh instantiation past that cache. */
function warmWasm(m: DetectorModule): Promise<unknown> {
  if (!wasm) {
    wasm = Promise.resolve(
      m.prepareZXingModule({ fireImmediately: true, equalityFn: () => false }),
    ).catch((e) => {
      wasm = null;
      throw e;
    });
  }
  return wasm;
}

/** The decoder: the ponyfill's glue plus its ~1 MB reader wasm, fetched once
 *  per page. The + sheet calls this the moment it opens, so by the time the
 *  user has picked Scan and granted the camera the reader is usually already
 *  here — that download behind the permission prompt was most of "the first
 *  scan is slow". A failed load is forgotten so the next call retries. */
export function loadDetector(): Promise<DetectorModule> {
  if (!detectorModule) {
    detectorModule = import("barcode-detector/ponyfill").catch((e) => {
      detectorModule = null;
      throw e;
    });
  }
  return detectorModule.then((m) => {
    warmWasm(m).catch(() => {
      /* detect() reports the failure when it runs; the next load retries */
    });
    return m;
  });
}
