"use client";

import { useRef, type ReactNode } from "react";
import { Close } from "./icons";
import { useSheetBack } from "./sheetBack";
import { useDragDismiss } from "./useDragDismiss";

/** The ✕ every sheet closes with, in every sheet's top-right corner: drawn,
 *  not a font glyph, so its weight and centring don't depend on the font.
 *  In a sheet head: 44px to the finger, a 34px disc to the eye
 *  (globals.css); the tray lip draws it at 30px. */
export function SheetClose({
  onClick,
  disabled,
  label = "Close",
}: {
  onClick: () => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <button
      type="button"
      className="sheet-close"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      <Close />
    </button>
  );
}

type Props = {
  title: ReactNode;
  /** for a dialog labelled by its title (`aria-labelledby`) */
  titleId?: string;
  onClose: () => void;
  /** false while closing would lose work in flight (the photo being
   *  prepared): the ✕ greys out, a pull springs back, and Escape and Back
   *  are refused rather than passed to the sheet underneath */
  canClose?: boolean;
  /** false for an action sheet (the + sheet), whose visible exit is its own
   *  Cancel row, like iOS's (Bill, 2026-10-13) */
  closeButton?: boolean;
};

/** A sub-sheet's top: a real grabber, the title and the ✕, pinned while the
 *  sheet scrolls. The whole zone pulls the sheet down to close it, and while
 *  this is the topmost sheet Escape and system Back close it too (sheetBack),
 *  all by the same `onClose`. */
export default function SheetHeader({
  title,
  titleId,
  onClose,
  canClose = true,
  closeButton = true,
}: Props) {
  const drag = useDragDismiss(onClose, canClose);
  const ref = useRef<HTMLDivElement>(null);
  useSheetBack(ref, () => {
    if (canClose) onClose();
  });
  return (
    <div className="subsheet-head" ref={ref} {...drag}>
      <div className="grabber" />
      <div className="sheet-head-row">
        <div className="subsheet-title" id={titleId}>
          {title}
        </div>
        {closeButton ? (
          <SheetClose onClick={onClose} disabled={!canClose} />
        ) : null}
      </div>
    </div>
  );
}

/** A form sheet's answer to a close that would lose its edits (Save as meal,
 *  a saved meal): in its foot, in place of Save — the inline confirm the
 *  phrase sheet's Delete mapping uses. */
export function DiscardConfirm({
  onKeep,
  onDiscard,
}: {
  onKeep: () => void;
  onDiscard: () => void;
}) {
  return (
    <div className="m3-actions">
      <span className="m3-note">Discard changes?</span>
      <button type="button" className="m3-link-btn" onClick={onKeep}>
        Keep editing
      </button>
      <button type="button" className="m3-link-btn danger" onClick={onDiscard}>
        Discard
      </button>
    </div>
  );
}
