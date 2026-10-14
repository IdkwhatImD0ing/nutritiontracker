"use client";

import { useEffect } from "react";

/**
 * Where the visible screen is, for the bottom sheets (globals.css), as two
 * :root properties. iOS (and Android Chrome by default) shrinks neither the
 * layout viewport nor dvh for the keyboard: it lays the keyboard over the
 * page and pans the visual viewport up to the focused field, so a sheet
 * fixed to the layout bottom keeps its height and the pan pushes its header
 * off the top. (`interactive-widget=resizes-content` would fix Android alone,
 * but it resizes every page, lifting the tab bar onto the keyboard.)
 *
 *   --vvh  the visual viewport's height: the screen above the keyboard
 *   --kb   the layout viewport's bottom edge to the visual viewport's —
 *          innerHeight − offsetTop − height. Unpanned that is the keyboard;
 *          fully panned it is 0, and offsetTop holds the keyboard instead.
 *
 * A sheet at `bottom: var(--kb)` whose height is capped by --vvh therefore
 * sits inside the visible screen however far iOS panned. With no keyboard
 * --kb is 0 and --vvh the full height, so nothing moves.
 *
 * Mounted once, in the root layout. Renders nothing. Writes only on a
 * change: both properties inherit everywhere, so each write restyles the
 * document, and pinch-zoom and toolbar moves fire these events in bursts.
 */
export function KeyboardInset() {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const root = document.documentElement.style;
    let last = "";
    const update = () => {
      const vvh = `${vv.height}px`;
      const kb = `${Math.max(0, window.innerHeight - vv.offsetTop - vv.height)}px`;
      if (vvh + kb === last) return;
      last = vvh + kb;
      root.setProperty("--vvh", vvh);
      root.setProperty("--kb", kb);
    };
    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
      root.removeProperty("--vvh");
      root.removeProperty("--kb");
    };
  }, []);
  return null;
}
