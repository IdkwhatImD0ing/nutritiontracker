"use client";

import type { ReactNode } from "react";
import { Close } from "./icons";
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
   *  prepared): the ✕ greys out and a pull springs back */
  canClose?: boolean;
  /** false for an action sheet (the + sheet), which closes by its backdrop
   *  or a pull, like iOS's own */
  closeButton?: boolean;
};

/** A sub-sheet's top: a real grabber, the title and the ✕, pinned while the
 *  sheet scrolls. The whole zone pulls the sheet down to close it. */
export default function SheetHeader({
  title,
  titleId,
  onClose,
  canClose = true,
  closeButton = true,
}: Props) {
  const drag = useDragDismiss(onClose, canClose);
  return (
    <div className="subsheet-head" {...drag}>
      <div className="grabber" />
      <div className="sheet-head-row">
        <h2 className="subsheet-title" id={titleId}>
          {title}
        </h2>
        {closeButton ? (
          <SheetClose onClick={onClose} disabled={!canClose} />
        ) : null}
      </div>
    </div>
  );
}
