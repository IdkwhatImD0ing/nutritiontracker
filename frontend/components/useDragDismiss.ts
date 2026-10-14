"use client";

import { useEffect, useRef, type PointerEvent } from "react";

/** How far a press travels before it is a drag: under it, a tap on the ✕ or
 *  the meal chip inside the zone is still a tap. */
const SLOP = 8;
/** Pulled further than this, the sheet goes on release. */
const DISMISS = 100;
/** Released faster than this (px/ms), it goes from a shorter pull. */
const FLICK = 0.5;
const SETTLE_MS = 200;

type Drag = {
  id: number;
  y: number;
  dy: number;
  t: number;
  v: number;
  sheet: HTMLElement;
  live: boolean;
};

/**
 * Pull a sheet down by its grabber zone to close it. Spread the result on
 * that zone only — globals.css gives it `touch-action: none`, which a
 * scrolling body must never get. Modelled on SplitSheet's divider: a ref, not
 * state, because nothing renders from the finger, and the sheet moves by its
 * `translate` property, which leaves the open animation's and the desktop
 * modal's `transform` alone.
 *
 * `onClose` must be the sheet's own close path, the one its ✕ takes (for
 * the staging sheet, Today's closeStaging). With `canDismiss` false the sheet
 * gives a quarter as far and always comes back, so the pull registers as
 * refused rather than ignored.
 */
export function useDragDismiss(onClose: () => void, canDismiss = true) {
  const drag = useRef<Drag | null>(null);
  const leaving = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Read when the slide-out ends, not at release: a guard inside onClose
  // (the Agent lane refuses while rows are staged) must see that moment's state.
  const close = useRef(onClose);
  close.current = onClose;

  // A sheet unmounted mid-exit (a backdrop tap, Escape) is already closed.
  useEffect(
    () => () => {
      if (leaving.current) clearTimeout(leaving.current);
    },
    [],
  );

  const end = (e: PointerEvent<HTMLElement>) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId) return;
    drag.current = null;
    if (!d.live) return;
    // a finger that stopped before it lifted is not a flick
    const v = e.timeStamp - d.t > 80 ? 0 : d.v;
    const go =
      canDismiss &&
      e.type === "pointerup" &&
      (d.dy > DISMISS || (v > FLICK && d.dy > SLOP * 3));
    const sheet = d.sheet;
    sheet.style.transition = `translate ${SETTLE_MS}ms var(--ease)`;
    sheet.style.translate = go ? "0 100%" : "";
    if (!go) return;
    leaving.current = setTimeout(() => {
      leaving.current = null;
      close.current();
      // A close that declined leaves the sheet mounted: bring it back on
      // screen. Checked a beat later, once React has had time to unmount a
      // sheet that did close — sooner, it would flash back for a frame.
      setTimeout(() => {
        if (sheet.isConnected) sheet.style.translate = "";
      }, SETTLE_MS);
    }, SETTLE_MS);
  };

  return {
    onPointerDown: (e: PointerEvent<HTMLElement>) => {
      if (!e.isPrimary || e.button !== 0 || leaving.current) return;
      const sheet = e.currentTarget.closest<HTMLElement>(".sheet, .subsheet");
      // Only where a grabber is drawn: from 900px the sheets are centred
      // modals and globals.css hides it.
      const grip = e.currentTarget.querySelector(".grabber");
      if (!sheet || !grip || grip.getClientRects().length === 0) return;
      drag.current = {
        id: e.pointerId,
        y: e.clientY,
        dy: 0,
        t: e.timeStamp,
        v: 0,
        sheet,
        live: false,
      };
    },
    onPointerMove: (e: PointerEvent<HTMLElement>) => {
      const d = drag.current;
      if (!d || d.id !== e.pointerId) return;
      const dy = Math.max(0, e.clientY - d.y);
      if (!d.live) {
        if (dy < SLOP) return;
        d.live = true;
        // captured only now, so a tap inside the zone still lands on its button
        e.currentTarget.setPointerCapture(e.pointerId);
        d.sheet.style.transition = "none";
      }
      if (e.timeStamp > d.t) d.v = (dy - d.dy) / (e.timeStamp - d.t);
      d.dy = dy;
      d.t = e.timeStamp;
      d.sheet.style.translate = `0 ${canDismiss ? dy : dy / 4}px`;
    },
    onPointerUp: end,
    onPointerCancel: end,
  };
}
