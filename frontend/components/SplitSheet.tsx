"use client";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { SheetClose } from "./SheetHeader";
import { useSheetBack } from "./sheetBack";
import { useDragDismiss } from "./useDragDismiss";
import { useSheetFocus } from "./useSheetFocus";

/** The Agent lane's one sheet (§7.1, Bill 2026-09-09: "I just wanna chat with
 *  the stage window open"). Before this, Review opened the staging sheet as a
 *  second 92dvh sheet stacked over the chat — so correcting a row meant open,
 *  read, close, type, open again. Now both live here: the staged list on top,
 *  the conversation under it, one composer at the bottom.
 *
 *  The divider drags, and lets go at one of three stops:
 *
 *    peek  the tray's lip and its Log button — the total stays in
 *          sight, and the chat has the rest of the screen
 *    half  a few rows and a working chat — where it opens, unless the
 *          sheet is short (below)
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
/** what the divider says, rather than a bare 20 / 48 / 80 */
const SAID: Record<Stop, string> = {
  peek: "List collapsed",
  half: "List half height",
  full: "List full height",
};

/** A sheet shorter than this (an SE; a mini only at Display Zoom, 320 wide)
 *  opens at `peek`: at `half` its chat was 118–165px, too little to show the
 *  reply the rows came with or the agent's follow-up question (owner,
 *  2026-10-13). It is the sheet's own height, so it counts the keyboard:
 *  rows that arrive while the keyboard is up open at peek on any phone. */
const SHORT_SHEET_PX = 640;

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
  /** the lane's ×, its backdrop, a pull on its header, Escape and Back. The
   *  staged list has its own discard (a trash glyph: it puts the rows away
   *  and leaves the conversation up); this one closes the lane, and with
   *  rows staged it closes the tray first (Today's closeLane). */
  onDismiss: () => void;
  /** each change grows the staged list one stop (peek → half → full): a
   *  chat card's Review on the log that is already here still opens it */
  expandSignal?: number;
  /** the tray's Log is in flight, so the lane cannot close (closeLane
   *  refuses): the ✕ greys out like the tray's trash, and a pull on the
   *  header springs back rather than sliding the sheet away and back */
  busy?: boolean;
};

export default function SplitSheet({
  stage,
  chat,
  onDismiss,
  expandSignal,
  busy = false,
}: Props) {
  const [split, setSplit] = useState<number>(STOPS.half);
  const [dragging, setDragging] = useState(false);
  // the dialog (the panes inside it are regions): focus moves in on open
  const shellRef = useSheetFocus<HTMLDivElement>();
  const titleId = useId();
  // Where the drag started. A ref, not state: it changes on every pointermove
  // and nothing renders from it.
  const from = useRef<{ id: number; y: number; at: number } | null>(null);

  // A staged list arrives at `half` however the last one was left — the rows
  // are the new thing on screen and deserve the room. On a short sheet it
  // arrives at `peek` instead: the lip still shows the meal, the total and
  // Log, and one tap on the divider shows the rows. Keyed on presence, so
  // dragging after it appears is not undone.
  const staged = stage !== null;
  useEffect(() => {
    // The tray coming or going can take the focused control with it (Log,
    // its trash, the toast's Undo): focus stays in this dialog, not the page.
    if (document.activeElement === document.body) {
      shellRef.current?.focus({ preventScroll: true });
    }
    // Gone, the split goes back to `half`, which has no rules of its own: a
    // `full` left behind would go on collapsing the chat with no tray above.
    if (!staged) {
      setSplit(STOPS.half);
      return;
    }
    const h = shellRef.current?.clientHeight ?? 0;
    setSplit(h > 0 && h < SHORT_SHEET_PX ? STOPS.peek : STOPS.half);
  }, [staged]);

  // Review on the log already staged: one stop more room for the list. Only
  // a change asks — the value it mounts with is a Review from an earlier
  // opening of the lane.
  const seenSignal = useRef(expandSignal);
  useEffect(() => {
    if (expandSignal === seenSignal.current) return;
    seenSignal.current = expandSignal;
    setSplit((f) => {
      const at = ORDER.indexOf(nearest(f));
      return STOPS[ORDER[Math.min(ORDER.length - 1, at + 1)]];
    });
  }, [expandSignal]);

  // A pull on the header closes the lane like its ✕, staged rows or not
  // (Bill, 2026-10-13), and is refused with it while Log is in flight. The
  // divider's drag is its own; it sits outside this header.
  const drag = useDragDismiss(onDismiss, !busy);
  // Escape and Back too, once the chat has had its turn (an attached photo
  // goes first) and with no sub-sheet stacked above
  useSheetBack(shellRef, onDismiss);

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
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        data-snap={dragging ? undefined : nearest(split)}
        // the slim title is for a staged lane only (globals.css .split-head)
        data-staged={staged || undefined}
        // the stage pane's flex-basis mid-drag; a custom property so a drag
        // repaints one declaration rather than re-rendering a stylesheet
        style={{ "--stage-split": `${split * 100}%` } as React.CSSProperties}
      >
        <div className="sheet-head split-head" {...drag}>
          <div className="grabber" />
          <div className="sheet-head-row">
            <h2 className="sheet-title" id={titleId}>
              Agent
            </h2>
            {/* §7.1 (Bill, 2026-10-13): always here, and it puts the whole
                lane away in one tap — with rows staged, through the tray's
                own close first (the §5 discard with its Undo). The tray's
                discard is a trash glyph, so the two never look alike. */}
            <SheetClose onClick={onDismiss} disabled={busy} />
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
            aria-valuetext={SAID[nearest(split)]}
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
