"use client";

import { SerwistProvider } from "@serwist/turbopack/react";
import type { ReactNode } from "react";

/**
 * Service-worker registration, pre-configured. @serwist/turbopack does NOT
 * auto-register the SW (unlike @serwist/next's webpack plugin) — this must be
 * mounted once, wrapping the root layout's children (one-line coordination
 * item with workstream V2, which owns the layout body):
 *
 *   import { ServiceWorkerProvider } from "./serwist/provider";
 *   <ServiceWorkerProvider>{children}</ServiceWorkerProvider>
 *
 * reloadOnOnline is off: Serwist's default reloads the page on every `online`
 * event, which on flapping mobile connectivity would wipe an in-progress
 * recording or an open staging sheet. Network recovery is handled by the
 * capture/drain flow (§8.6), not by reloading.
 *
 * cacheOnNavigation is off: Serwist's default patches history.pushState /
 * replaceState so every client-side navigation posts CACHE_URLS to the SW,
 * which re-fetches the URL as a bare `new Request(url)`. That request carries
 * no `RSC` header, so it misses both RSC matchers in defaultCache (app/sw.ts)
 * and falls through to the same-origin NetworkFirst catch-all — a second full
 * document GET, and a proxy (middleware) invocation with it, on every tab
 * switch. What that buys is a warm document entry for an offline reload of the
 * route just navigated to — and offline is a deliberate non-feature (§8.6), so
 * it is not worth a duplicate round trip on every navigation.
 */
export function ServiceWorkerProvider({ children }: { children: ReactNode }) {
  return (
    <SerwistProvider swUrl="/serwist/sw.js" cacheOnNavigation={false} reloadOnOnline={false}>
      {children}
    </SerwistProvider>
  );
}
