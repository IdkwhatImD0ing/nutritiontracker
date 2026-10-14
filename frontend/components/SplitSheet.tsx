"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { SheetClose } from "./SheetHeader";
import { useDragDismiss } from "./useDragDismiss";

/** The Agent lane's one sheet (§7.1, Bill 2026-09-09: "I just wanna chat with
 *  the stage window open"). Before this, Review opened the staging sheet as a
 *  second 92dvh sheet stacked over the chat — so correcting a row meant open,
 *  read, close, type, open again. Now both live here: the staged list on top,
 *  the conversation under it, one composer at the bottom.
 *
 *  The divider drags, and lets go at one of three stops:
 *
 *    peek  the staged header and its Commit button — the total stays in
 *          sight, and the chat has the rest of the screen
 *    half  a few rows and a working chat — where it opens
 *    full  the whole staged list; the chat keeps its composer
 *
 *  While the finger is down the split is a free fraction of the sheet's own
 *  height (`--stage-split`), so the divider tracks it. On release it settles
 *  on the nearest stop and `data-snap` takes over, because peek and full are
 *  about a pane's CHROME — a header plus a button, a composer — and chrome is
 *  a fixed number of pixels that no fraction gets right on both a small phone
 *  and a large one. CSS collapses the other pane's body at those two stops.
 *
 *  `stage` is null when nothing is staged, and then this is just the chat in a
 *  sheet. The chat is ALWAYS this component's child either way: moving it in
 *  and out of the tree as rows come and go would remount it, and a remounted
 *  AgentChat clears the composer and re-runs the mount effect that decides
 *  between opening the mic and opening the photo picker. */

const STOPS = { peek: 0.2, half: 0.48, full: 0.8 } as const;
type Stop = keyof typeof STOPS;
const ORDER: Stop[] = ["peek", "half", "full"];

function nearest(f: number): Stop {
  return ORDER.reduce((best, s) =>
    Math.abs(STOPS[s] - f) < Math.abs(STOPS[best] - f) ? s : best,
  );
}

type Props = {
  /** the staged list, rendered `docked`; null when nothing is staged */
  stage: ReactNode | null;
  /** the conversation, rendered `docked` — always present */
  chat: ReactNode;
  /** the lane's ×, its backdrop and a pull on its header. The staged list
   *  has its own × (it puts the rows away and leaves the conversation up);
   *  this one closes the lane. */
  onDismiss: () => void;
};

export default function SplitSheet({ stage, chat, onDismiss }: Props) {
  const [split, setSplit] = useState<number>(STOPS.half);
  const [dragging, setDragging] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  // Where the drag started. A ref, not state: it changes on every pointermove
  // and nothing renders from it.
  const from = useRef<{ id: number; y: number; at: number } | null>(null);

  // A staged list arrives at `half` however the last one was left — the rows
  // are the new thing on screen and deserve the room. Keyed on presence, so
  // dragging after it appears is not undone.
  const staged = stage !== null;
  useEffect(() => {
    if (staged) setSplit(STOPS.half);
  }, [staged]);

  // A pull on the header closes the lane like its ✕ — and, like that ✕, not
  // while rows are staged: that would be a discard, which is the tray's ✕'s.
  // The divider's drag is its own; it sits outside this header.
  const drag = useDragDismiss(onDismiss, !staged);

  const end = () => {
    if (!from.current) return;
    from.current = null;
    setDragging(false);
    setSplit((f) => STOPS[nearest(f)]);
  };

  const go = (to: Stop) => setSplit(STOPS[to]);

  return (
    <>
      <div className="agent-backdrop" onClick={onDismiss} />
      <div
        className="sheet split-sheet"
        ref={shellRef}
        data-snap={dragging ? undefined : nearest(split)}
        // the stage pane's flex-basis mid-drag; a custom property so a drag
        // repaints one declaration rather than re-rendering a stylesheet
        style={{ "--stage-split": `${split * 100}%` } as React.CSSProperties}
      >
        <div className="sheet-head split-head" {...drag}>
          <div className="grabber" />
          <div className="sheet-head-row">
            <span className="sheet-title">Agent</span>
            {/* While rows are staged the staged list's own × is the one that
                acts — it puts the rows away (§5 discard, undoable) and this
                one comes back. Two ×es, one of them inert, would be worse
                than one. */}
            {staged ? null : <SheetClose onClick={onDismiss} />}
          </div>
        </div>

        {stage}

        {staged ? (
          <div
            className="split-divider"
            role="separator"
            aria-label="Resize the list"
            aria-orientation="horizontal"
            aria-valuenow={Math.round(split * 100)}
            aria-valuemin={Math.round(STOPS.peek * 100)}
            aria-valuemax={Math.round(STOPS.full * 100)}
            tabIndex={0}
            onPointerDown={(e) => {
              from.current = { id: e.pointerId, y: e.clientY, at: split };
              setDragging(true);
              e.currentTarget.setPointerCapture(e.pointerId);
            }}
            onPointerMove={(e) => {
              const d = from.current;
              const h = shellRef.current?.clientHeight ?? 0;
              if (!d || d.id !== e.pointerId || h === 0) return;
              setSplit(
                Math.min(
                  STOPS.full,
                  Math.max(STOPS.peek, d.at + (e.clientY - d.y) / h),
                ),
              );
            }}
            onPointerUp={end}
            onPointerCancel={end}
            // A drag handle with no keyboard path is not a separator control.
            onKeyDown={(e) => {
              const at = ORDER.indexOf(nearest(split));
              if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
                go(ORDER[Math.max(0, at - 1)]);
              } else if (e.key === "ArrowDown" || e.key === "ArrowRight") {
                go(ORDER[Math.min(ORDER.length - 1, at + 1)]);
              } else {
                return;
              }
              e.preventDefault();
            }}
            // one tap cycles peek → half → full → peek, for anyone who would
            // rather not drag
            onClick={() => {
              if (from.current) return;
              go(ORDER[(ORDER.indexOf(nearest(split)) + 1) % ORDER.length]);
            }}
          >
            <span className="split-grip" aria-hidden="true" />
          </div>
        ) : null}

        {chat}
      </div>
      {/* The stage's own sub-sheets (portion, manual search, food edit) and
          the chat's photo picker ride inside `stage` / `chat`: they are
          position:fixed `.subsheet`es, out of this flex flow, and their
          z-index puts them over the split. */}
    </>
  );
}
