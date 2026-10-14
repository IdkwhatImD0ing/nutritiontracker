"use client";

import { useLayoutEffect, type RefObject } from "react";

/** A textarea that grows with what is in it instead of scrolling inside a
 *  fixed box. Each time `value` changes the height is reset, then set to the
 *  content's (plus the borders: scrollHeight leaves them out, and fields are
 *  border-box). `rows` and any CSS min-height stay the floor, since the reset
 *  box is never shorter than they make it. The field's CSS should set
 *  `overflow: hidden` and `resize: none`. CSS `field-sizing: content` would
 *  do this without JS; iOS Safari support for it is unverified.
 *
 *    const ref = useRef<HTMLTextAreaElement>(null);
 *    useAutoGrow(ref, text);
 *    <textarea ref={ref} rows={2} value={text} … />
 */
export function useAutoGrow(
  ref: RefObject<HTMLTextAreaElement | null>,
  value: string,
): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + el.offsetHeight - el.clientHeight}px`;
  }, [ref, value]);
}
