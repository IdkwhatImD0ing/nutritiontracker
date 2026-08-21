/// <reference lib="esnext" />
/// <reference lib="webworker" />
import { defaultCache } from "@serwist/turbopack/worker";
import type { PrecacheEntry, SerwistGlobalConfig } from "serwist";
import { NetworkOnly, Serwist } from "serwist";

// `injectionPoint` for the precache manifest: replaced at build time by
// @serwist/turbopack's route handler (app/serwist/[path]/route.ts).
declare global {
  interface WorkerGlobalScope extends SerwistGlobalConfig {
    __SW_MANIFEST: (PrecacheEntry | string)[] | undefined;
  }
}

declare const self: ServiceWorkerGlobalScope;

const serwist = new Serwist({
  precacheEntries: self.__SW_MANIFEST,
  skipWaiting: true,
  clientsClaim: true,
  navigationPreload: true,
  runtimeCaching: [
    // §8.6: offline capture yes, offline resolution no — /api responses are
    // NEVER cached. Routing is first-match-wins, so this rule must stay ahead
    // of defaultCache (whose same-origin entries would otherwise NetworkFirst-
    // cache /api). Covers FastAPI's /api/* and the Next /api/auth/* handler.
    {
      matcher: ({ sameOrigin, url }) => sameOrigin && url.pathname.startsWith("/api/"),
      handler: new NetworkOnly(),
    },
    // App shell + static assets: Serwist's Next.js defaults (documents/RSC
    // NetworkFirst, build-hashed statics cache-friendly) — correct here
    // because everything non-/api is safe to serve stale on a reopen.
    ...defaultCache,
  ],
});

serwist.addEventListeners();
