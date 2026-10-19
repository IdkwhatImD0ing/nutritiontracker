"use client";

import { useEffect, useRef, type RefObject } from "react";

/* System Back and Escape for open sheets. Before this, Back with a sheet up
   left the tab, and a staged log went with it — no "Log discarded · Undo".

   Every open sheet registers here (SheetHeader does it for each sub-sheet),
   and ONE history entry, pushed at the page's own URL, stands for the whole
   stack. Back pops that entry, and the topmost sheet closes the way its ✕
   does — for the staging sheet that is Today's closeStaging, so the Undo
   toast comes up. If sheets are still open after it, the entry goes back on.
   Escape takes the same path, so the two never disagree.

   Topmost: a sub-sheet over a full-height sheet, else the one latest in the
   document — a sub-sheet renders after the sheet that opened it. A sheet
   playing its exit (inside [data-closing], useSheetPresence) is already
   gone. A handler that returns false passes to the next one down: the Agent
   chat drops an attached photo first, and otherwise the lane closes.

   A sheet closed any other way (its ✕, the backdrop, a commit) unregisters,
   and the last one out steps back over the entry: a tick later, only if no
   sheet registered meanwhile and the entry is still the current one. One
   entry for the stack, not one per sheet, is what survives the hand-offs
   that happen inside one tick — the + sheet giving way to a lane, Take photo
   opening the chat and its picker, Edit food giving way to Find a food —
   since history.back() lands asynchronously and would pop the next sheet's
   entry. Next's router lets an entry pushed with its own state through
   untouched, and a pop back onto the page's entry is a same-URL restore. */

const KEY = "tallySheet";

type Entry = {
  el: RefObject<HTMLElement | null>;
  back: { current: () => boolean | void };
};

const stack: Entry[] = [];
/** the id our entry carries, while that entry is the current one */
let armed: number | null = null;
let seq = 0;
let tick: ReturnType<typeof setTimeout> | undefined;
let listening = false;

/** the open sheets, topmost first */
function ranked(): Entry[] {
  const up = stack.filter((e) => {
    const el = e.el.current;
    return el != null && el.isConnected && !el.closest("[data-closing]");
  });
  // The tray's open meal picker ranks with the sub-sheets: in the Agent
  // split the chat pane comes after it in the document, and would otherwise
  // answer first by dropping an attached photo under the open picker.
  const sub = (e: Entry) =>
    e.el.current!.closest(".subsheet, .meal-picker") ? 1 : 0;
  return up.sort(
    (a, b) =>
      sub(b) - sub(a) ||
      (a.el.current!.compareDocumentPosition(b.el.current!) &
      Node.DOCUMENT_POSITION_FOLLOWING
        ? 1
        : -1),
  );
}

function dispatch() {
  for (const e of ranked()) if (e.back.current() !== false) return;
}

function sync() {
  tick = undefined;
  const up = ranked().length > 0;
  if (up && armed == null) {
    armed = ++seq;
    history.pushState({ ...history.state, [KEY]: armed }, "");
  } else if (!up && armed != null) {
    const ours = history.state?.[KEY] === armed;
    armed = null;
    if (ours) history.back();
  }
}

function later() {
  clearTimeout(tick);
  tick = setTimeout(sync, 0);
}

function listen() {
  if (listening) return;
  listening = true;
  window.addEventListener("popstate", () => {
    // not ours to answer: a page's own back or forward, no sheet open
    if (armed == null) return;
    armed = null; // Back has already taken the entry
    dispatch();
    later();
  });
  window.addEventListener("keydown", (e) => {
    // a held key closes one sheet, not the whole stack under it
    if (e.key === "Escape" && !e.repeat) dispatch();
  });
}

/** Back and Escape close this sheet while `enabled`. `onBack` is the sheet's
 *  own close, the one its ✕ takes, or returns false to pass to the sheet
 *  underneath. `ref` is any element inside the sheet: where it sits in the
 *  document is what ranks it. */
export function useSheetBack(
  ref: RefObject<HTMLElement | null>,
  onBack: () => boolean | void,
  enabled = true,
) {
  const back = useRef(onBack);
  back.current = onBack;
  useEffect(() => {
    if (!enabled) return;
    const entry: Entry = { el: ref, back };
    stack.push(entry);
    listen();
    later();
    return () => {
      stack.splice(stack.indexOf(entry), 1);
      later();
    };
  }, [ref, enabled]);
}

/** A pick that also navigates (TabBar's, from another tab) gives the entry to
 *  the navigation. True when it is the current one: the caller replaces it,
 *  rather than this stepping back over it and the navigation pushing past. */
export function releaseForNavigation(): boolean {
  clearTimeout(tick);
  const ours = armed != null && history.state?.[KEY] === armed;
  armed = null;
  return ours;
}
