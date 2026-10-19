"use client";

import { createContext, useEffect, useRef, useState, type ReactNode } from "react";

/** How long a closed sheet stays mounted: globals.css's sheet-down, fade-out
 *  and modal-out run this long. A timer, not animationend — the sheet's own
 *  children bubble theirs, and under reduced motion there is next to none. */
export const EXIT_MS = 220;

/** For a sheet that takes an `open` prop (AddSheet, PhotoCapture): still
 *  `mounted` for EXIT_MS after `open` goes false, and `closing` meanwhile. */
export function usePresence(open: boolean) {
  const [kept, setKept] = useState(open);
  useEffect(() => {
    if (open) {
      setKept(true);
      return;
    }
    const t = setTimeout(() => setKept(false), EXIT_MS);
    return () => clearTimeout(t);
  }, [open]);
  const closing = !open && kept;
  return { mounted: open || closing, closing };
}

/** True inside a sheet playing its exit (SheetFrame). useSheetFocus reads it
 *  to hand focus back as the exit starts, not EXIT_MS later at unmount, by
 *  which time a sheet the same tap opened (Edit food giving way to Search)
 *  has taken focus and recorded the leaving sheet's button as its opener. */
export const SheetClosing = createContext(false);

/** What a sheet closes inside: no box of its own (display: contents), and
 *  while `closing` it carries [data-closing] — the sheet slides down and its
 *  scrim fades (globals.css) — and is inert, so nothing in it takes a tap or
 *  keeps focus on its way out. */
export function SheetFrame({
  closing,
  children,
}: {
  closing: boolean;
  children: ReactNode;
}) {
  return (
    <div
      className="sheet-frame"
      data-closing={closing ? "" : undefined}
      inert={closing}
    >
      <SheetClosing.Provider value={closing}>{children}</SheetClosing.Provider>
    </div>
  );
}

/** For a sheet rendered conditionally (`{x ? <Sheet/> : null}`): wrap the
 *  condition, and the last sheet stays on screen through its exit at the
 *  same place in the tree, its state intact. A sheet opened while another
 *  is still leaving mounts fresh, as it always did. */
export default function SheetPresence({ children }: { children: ReactNode }) {
  const open = children != null && children !== false;
  const last = useRef<ReactNode>(null);
  const opened = useRef(0);
  const wasOpen = useRef(false);
  if (open) {
    if (!wasOpen.current) opened.current += 1;
    last.current = children;
  }
  wasOpen.current = open;
  const { mounted, closing } = usePresence(open);
  if (!mounted) return null;
  return (
    <SheetFrame key={opened.current} closing={closing}>
      {open ? children : last.current}
    </SheetFrame>
  );
}
