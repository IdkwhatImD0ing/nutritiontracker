"use client";

import { useEffect, useRef } from "react";

/**
 * Focus for a sheet (an `aria-modal` dialog): on open it moves into the
 * sheet, on close it goes back to whatever opened it. Put the ref on the
 * dialog element, with `tabIndex={-1}` so the element can take focus.
 *
 * A field that focused itself (autoFocus) keeps focus, and a later
 * programmatic focus simply wins. On close, focus returns only if it was left
 * nowhere — on the page body, or inside the sheet being closed — so a close
 * that hands focus somewhere on purpose (the next sheet, ApiKeysCard's own
 * button) keeps it. Escape and system Back belong to SheetHeader, not here.
 */
export function useSheetFocus<T extends HTMLElement>(active = true) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const sheet = ref.current;
    if (!active || !sheet) return;
    const opener = document.activeElement;
    if (!sheet.contains(opener)) sheet.focus({ preventScroll: true });
    return () => {
      const now = document.activeElement;
      const lost = !now || now === document.body || sheet.contains(now);
      if (lost && opener instanceof HTMLElement && opener.isConnected && !sheet.contains(opener)) {
        opener.focus({ preventScroll: true });
      }
    };
  }, [active]);
  return ref;
}
