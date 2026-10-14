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
 * Not covered: a browser back or forward between two tab roots (a desktop
 * browser, Android's back gesture). The browser can put back the scroll of
 * the entry it lands on before React swaps the page, and the page being
 * left would then save that offset instead of its own. The installed app
 * has no browser back; its pops are BackBar's, from screens that aren't
 * tab roots.
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
  // offset it had.
  useLayoutEffect(
    () => () => {
      if (restored.current) saved.set(pathname, window.scrollY);
    },
    [pathname],
  );
}
