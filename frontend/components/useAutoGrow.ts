"use client";

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

/** A textarea that grows with what is in it instead of scrolling inside a
 *  fixed box. Each time `value` changes the height is reset, then set to the
 *  content's (plus the borders: scrollHeight leaves them out, and fields are
 *  border-box). `rows` and any CSS min-height stay the floor, since the reset
 *  box is never shorter than they make it. The field's CSS should set
 *  `overflow: hidden` and `resize: none`. CSS `field-sizing: content` would
 *  do this without JS; iOS Safari support for it is unverified.
 *
 *  The same text wraps onto more lines in a narrower box (a phone turned
 *  upright, a window narrowed), which `overflow: hidden` would cut off, so a
 *  resize that changes the box's width fits it again too. A resize that
 *  leaves the width (a keyboard opening on Android) does nothing.
 *
 *    const ref = useRef<HTMLTextAreaElement>(null);
 *    useAutoGrow(ref, text);
 *    <textarea ref={ref} rows={2} value={text} … />
 */
export function useAutoGrow(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
): void {
  // the width the height was last fitted at
  const fittedAt = useRef(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    fit(el);
    fittedAt.current = el.clientWidth;
  }, [ref, value]);
  useEffect(() => {
    const onResize = () => {
      const el = ref.current;
      if (!el || el.clientWidth === fittedAt.current) return;
      fit(el);
      fittedAt.current = el.clientWidth;
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [ref]);
}

function fit(el: HTMLTextAreaElement) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
}
