"use client";

import { useContext, useEffect, useRef } from "react";
import { SheetClosing } from "./useSheetPresence";

/** A sheet that closes as the next one opens, focus already in the next (the
 *  + sheet as the Search lane's field focuses itself), leaves its opener here
 *  under the next sheet's element: that sheet's effect runs after its field
 *  took focus, so it cannot see who opened it. */
const handedOver = new WeakMap<Element, Element>();

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
  // A sheet playing its exit (SheetPresence keeps it mounted) is closed as
  // far as focus goes: the opener gets focus back now, before a sheet opened
  // by the same tap records who opened it.
  const closing = useContext(SheetClosing);
  const on = active && !closing;
  useEffect(() => {
    const sheet = ref.current;
    if (!on || !sheet) return;
    const focused = document.activeElement;
    // an autoFocus field took focus as the sheet mounted, before this ran
    const autoFocused = sheet.contains(focused);
    const opener = autoFocused ? handedOver.get(sheet) : focused;
    if (!autoFocused) sheet.focus({ preventScroll: true });
    return () => {
      if (!(opener instanceof HTMLElement) || !opener.isConnected || sheet.contains(opener)) return;
      const now = document.activeElement;
      // Lost: on the body, on a sheet's own frame (a tap on a backdrop
      // inside the Agent split focuses the split), or still inside this sheet
      // as it plays its exit (on the page, inert, for EXIT_MS). A
      // self-focused field still holding focus in a sheet that is staying is
      // StrictMode's rehearsal unmount, not a close: it keeps focus.
      const leaving = sheet.closest("[data-closing]") != null;
      const lost =
        !now ||
        now === document.body ||
        now.matches('[aria-modal="true"]') ||
        ((leaving || !autoFocused) && sheet.contains(now));
      if (lost) {
        opener.focus({ preventScroll: true });
      } else {
        const next = now.closest('[aria-modal="true"]');
        if (next) handedOver.set(next, opener);
      }
    };
  }, [on]);
  return ref;
}
