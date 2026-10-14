"use client";

import { usePathname } from "next/navigation";
import { useLayoutEffect, useRef } from "react";

/* Where each tab root was scrolled to when it last unmounted, by pathname,
   for as long as the document lasts. */
const saved = new Map<string, number>();

/**
 * A tab root's scroll position across tab switches and drill-ins. Each tab
 * is its own route, so leaving one unmounts it: this saves window.scrollY on
 * the way out and, once `ready` (the page's content is on screen at full
 * height, usually straight from readCache), scrolls back there before paint,
 * or to the top when nothing is saved. The tab bar's links pass
 * scroll={false}, so Next's own scroll-to-top doesn't undo it. Restores once
 * per mount: a later re-read of the data leaves the reader where they are.
 *
 * A tab root left by a back or forward (a lane cancelled back to its tab,
 * Today's router.back(); a browser button; Android's back gesture) saves
 * the offset it had when the popstate fired: the browser then puts back the
 * scroll of the entry it lands on (HTML's "restore persisted state" follows
 * popstate) before React swaps the page, so a read at unmount would save
 * that entry's offset instead of this page's.
 */
export function useScrollMemory(ready: boolean): void {
  const pathname = usePathname();
  const restored = useRef(false);

  useLayoutEffect(() => {
    if (restored.current) return;
    if (!ready) {
      // the page we came from may have been scrolled; its offset means
      // nothing here
      window.scrollTo(0, 0);
      return;
    }
    restored.current = true;
    window.scrollTo(0, saved.get(pathname) ?? 0);
  }, [ready, pathname]);

  // A layout cleanup runs before React removes the page, while the document
  // still has its height. A page left before its content showed keeps the
  // offset it had. A pop away from this page is read as it fires (above);
  // one that stays on it (a sheet's own entry) is not a leaving.
  useLayoutEffect(() => {
    let leftAt: number | null = null;
    const onPop = () => {
      if (location.pathname !== pathname) leftAt = window.scrollY;
    };
    window.addEventListener("popstate", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      if (restored.current) saved.set(pathname, leftAt ?? window.scrollY);
    };
  }, [pathname]);
}
