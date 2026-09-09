"use client";

import { useEffect, useRef } from "react";

/** Two events raised by one return to the foreground arrive together; the
 *  second is a duplicate, not a second resume. */
const COALESCE_MS = 500;

/**
 * Fires when the app comes back to the foreground, with how long it was away.
 *
 * Nothing in this app listened for `visibilitychange`, `pageshow` or the iOS
 * freeze/resume pair, and every surface reads its data once in a mount effect.
 * On the web that is invisible — a reload is a keystroke away. Installed it is
 * not: the manifest is `display: standalone` (no reload button) and
 * globals.css turns off overscroll (no pull-to-refresh, deliberately, for the
 * rubber-band). So a phone that keeps Tally warm in the background showed the
 * same diary, and after local midnight the wrong day's diary, with no way out
 * but force-quitting the app.
 *
 * The caller decides what "away long enough to matter" means; a quick tab-out
 * and back reports a few hundred ms and normally costs nothing.
 *
 * Two events, because neither alone is enough on iOS: `visibilitychange` is
 * the ordinary background/foreground pair, and `pageshow` with
 * `persisted: true` is a restore from the back/forward cache, which does not
 * always emit a visibility change. A bfcache restore reports `Infinity` —
 * the page may have been frozen for days, and there is no timestamp to
 * subtract that would mean anything.
 */
export function useOnForeground(onForeground: (awayMs: number) => void) {
  const cb = useRef(onForeground);
  cb.current = onForeground;

  useEffect(() => {
    let hiddenAt: number | null =
      document.visibilityState === "hidden" ? Date.now() : null;
    let lastFire = 0;

    // One return to the foreground can raise both events: a bfcache restore
    // emits `pageshow` AND flips visibility, and each on its own passes the
    // caller's staleness gate, so every resume fetch would go out twice.
    const fire = (away: number) => {
      const now = Date.now();
      if (now - lastFire < COALESCE_MS) return;
      lastFire = now;
      cb.current(away);
    };

    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
        return;
      }
      const away = hiddenAt == null ? 0 : Date.now() - hiddenAt;
      hiddenAt = null;
      fire(away);
    };

    const onPageShow = (e: PageTransitionEvent) => {
      if (e.persisted) fire(Number.POSITIVE_INFINITY);
    };

    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("pageshow", onPageShow);
    };
  }, []);
}
