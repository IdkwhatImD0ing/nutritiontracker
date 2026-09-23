"use client";

import { useId, useRef, useState, type KeyboardEvent } from "react";
import { CopyButton } from "./CopyButton";

export interface CodeTab {
  label: string;
  lang: string;
  code: string;
}

/** The APG tabs keyboard model, shared by CodeTabs and McpSetupCard: one tab
 *  in the page's Tab order (roving tabindex), Left/Right move and select,
 *  Home/End jump to the ends. Selection follows focus because every panel's
 *  content is already on the page. */
export function useRovingTabs(count: number, active: number, select: (i: number) => void) {
  const listRef = useRef<HTMLDivElement>(null);

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const moves: Record<string, number> = {
      ArrowRight: active + 1,
      ArrowLeft: active - 1,
      Home: 0,
      End: count - 1,
    };
    if (!(e.key in moves)) return;
    e.preventDefault();
    const next = (moves[e.key] + count) % count;
    select(next);
    listRef.current?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus();
  }

  return { listRef, onKeyDown };
}

/** The same request in several languages. Every panel is rendered (inactive
 *  ones `hidden`), so the static HTML carries all of them; Copy copies the
 *  one on show. */
export function CodeTabs({ tabs, label }: { tabs: CodeTab[]; label: string }) {
  const id = useId();
  const [active, setActive] = useState(0);
  const { listRef, onKeyDown } = useRovingTabs(tabs.length, active, setActive);
  const current = tabs[active];

  return (
    <div className="docs-code">
      <div className="docs-code-bar">
        <div
          ref={listRef}
          role="tablist"
          aria-label={label}
          className="docs-tabs"
          onKeyDown={onKeyDown}
        >
          {tabs.map((t, i) => (
            <button
              key={t.label}
              type="button"
              role="tab"
              id={`${id}-tab-${i}`}
              aria-selected={i === active}
              aria-controls={`${id}-panel-${i}`}
              tabIndex={i === active ? 0 : -1}
              className="docs-tab"
              onClick={() => setActive(i)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <CopyButton text={current.code} label={`${current.label} example`} />
      </div>
      {tabs.map((t, i) => (
        <div
          key={t.label}
          role="tabpanel"
          id={`${id}-panel-${i}`}
          aria-labelledby={`${id}-tab-${i}`}
          hidden={i !== active}
        >
          <pre tabIndex={0} aria-label={`${t.label} code`}>
            <code>{t.code}</code>
          </pre>
        </div>
      ))}
    </div>
  );
}
