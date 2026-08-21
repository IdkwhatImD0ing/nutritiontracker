import { createSerwistRoute } from "@serwist/turbopack";

/**
 * Serwist's Turbopack integration: this route handler bundles app/sw.ts with
 * esbuild at build time, injects the precache manifest (the build-hashed app
 * shell it discovers in the build output), and statically serves the result
 * at /serwist/sw.js (+ source map). Precached URLs are content-hashed, so
 * they self-version — no additionalPrecacheEntries/revision bookkeeping yet.
 */
export const { dynamic, dynamicParams, revalidate, generateStaticParams, GET } =
  createSerwistRoute({
    swSrc: "app/sw.ts",
    useNativeEsbuild: true,
  });
