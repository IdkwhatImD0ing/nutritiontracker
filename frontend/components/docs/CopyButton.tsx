"use client";

import { useEffect, useRef, useState } from "react";
import { writeClipboard } from "@/lib/clipboard";

type State = "idle" | "copied" | "failed";

/** "Copy" for a code sample. The button's own text flips to "Copied"; the
 *  polite live region beside it is what a screen reader hears. `label` names
 *  what is copied, since a page has several of these buttons; `idleText`
 *  replaces the visible "Copy" where two buttons share one bar (the landing's
 *  terminal panel). The live region is the global .sr-only, so the button
 *  also works off the docs routes, where docs.css isn't loaded. */
export function CopyButton({
  text,
  label,
  idleText = "Copy",
}: {
  text: string;
  label: string;
  idleText?: string;
}) {
  const [state, setState] = useState<State>("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  async function copy() {
    let next: State = "copied";
    try {
      await writeClipboard(text);
    } catch {
      next = "failed";
    }
    setState(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 1600);
  }

  const said = state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "";
  return (
    <>
      <button
        type="button"
        className={`docs-copy${state === "copied" ? " done" : ""}`}
        onClick={copy}
        aria-label={`Copy ${label}`}
      >
        {said || idleText}
      </button>
      <span className="sr-only" aria-live="polite">
        {said}
      </span>
    </>
  );
}
