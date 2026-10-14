"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/** A message that clears itself `ms` after it is shown — a "Saved." or a
 *  confirmation toast that would otherwise sit on screen until the next
 *  visit. Today's commit note (committedTimer) is the same pattern: a new
 *  message restarts the clock, `flash(null)` clears it at once (an OK
 *  button), and unmounting cancels the timer.
 *
 *    const [note, flash] = useFlash(4000);
 *    flash("Saved as a meal.");
 */
export function useFlash(ms: number): [string | null, (msg: string | null) => void] {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  const flash = useCallback(
    (next: string | null) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
      setMsg(next);
      if (next == null) return;
      timer.current = setTimeout(() => {
        timer.current = null;
        setMsg(null);
      }, ms);
    },
    [ms],
  );

  return [msg, flash];
}
