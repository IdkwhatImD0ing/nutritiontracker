"use client";

import { useEffect, useRef, useState } from "react";

type State = "idle" | "copied" | "failed";

/** The async Clipboard API needs a secure context and can still be refused
 *  (permissions, an embedded frame); a selected hidden textarea plus
 *  execCommand("copy") is the fallback that works in those places. */
async function writeClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // fall through to the legacy path
    }
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  area.setSelectionRange(0, text.length); // iOS Safari ignores select() alone
  const ok = document.execCommand("copy");
  area.remove();
  if (!ok) throw new Error("copy refused");
}

/** "Copy" for a code sample. The button's own text flips to "Copied"; the
 *  polite live region beside it is what a screen reader hears. `label` names
 *  what is copied, since a page has several of these buttons. */
export function CopyButton({ text, label }: { text: string; label: string }) {
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
        {said || "Copy"}
      </button>
      <span className="docs-sr" aria-live="polite">
        {said}
      </span>
    </>
  );
}
