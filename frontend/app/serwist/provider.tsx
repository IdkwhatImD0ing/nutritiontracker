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
 */
export function ServiceWorkerProvider({ children }: { children: ReactNode }) {
  return (
    <SerwistProvider swUrl="/serwist/sw.js" reloadOnOnline={false}>
      {children}
    </SerwistProvider>
  );
}
