import { createSerwistRoute } from "@serwist/turbopack";

/**
 * Serwist's Turbopack integration: this route handler bundles app/sw.ts with
 * esbuild at build time, injects the precache manifest (the build-hashed app
 * shell it discovers in the build output), and statically serves the result
 * at /serwist/sw.js (+ source map). Precached URLs are content-hashed, so
 * they self-version — no additionalPrecacheEntries/revision bookkeeping yet.
 */
/**
 * Everything under public/ lands in the precache by default, which for the
 * social card and the install-dialog screenshots is pure waste: only Chrome's
 * install UI and link unfurlers ever request them, and neither goes through the
 * service worker. Leaving them in cost ~250 KiB on every SW install (the
 * precache jumped 1358 -> 1577 KiB when they were added). The iOS launch
 * images (splash/, ~256 KiB) go too: a phone only ever uses the one that
 * matches its screen, Android and desktop none. The icons stay — those the
 * installed app really does use.
 *
 * Matched against the on-disk path, not the served URL: the manifest is globbed
 * out of `public/**` relative to the project root, and the leading slash is only
 * put on afterwards. A `/og.png` pattern here silently matches nothing.
 */
const NEVER_FETCHED_BY_THE_APP = /^\/?(public\/)?(og\.png|screenshots\/|splash\/)/;

export const { dynamic, dynamicParams, revalidate, generateStaticParams, GET } =
  createSerwistRoute({
    swSrc: "app/sw.ts",
    useNativeEsbuild: true,
    manifestTransforms: [
      (entries) => ({
        manifest: entries.filter((e) => !NEVER_FETCHED_BY_THE_APP.test(e.url)),
      }),
    ],
  });
