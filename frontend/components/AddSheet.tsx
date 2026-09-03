"use client";

import { useEffect } from "react";

/** The + sheet (owner ruling, 2026-09-02): the mid-screen capture bar is
 *  gone; every way to log opens from the tab bar's + button. Exactly four
 *  rows, in this order. The sheet only reports the pick — TabBar routes it
 *  to Today (a window event while Today is mounted, a sessionStorage handoff
 *  plus a navigation home when it isn't), and Today mounts the lane's sheet. */

export const ADD_LANES = ["barcode", "photo", "search", "voice"] as const;
export type AddLane = (typeof ADD_LANES)[number];

/** window CustomEvent<AddLane> TabBar dispatches while Today is mounted */
export const ADD_EVENT = "tally:add";
/** sessionStorage key carrying the pick across a navigation to Today */
export const ADD_HANDOFF_KEY = "tally.add.v1";

export function isAddLane(v: unknown): v is AddLane {
  return typeof v === "string" && (ADD_LANES as readonly string[]).includes(v);
}

type Props = {
  open: boolean;
  onClose: () => void;
  onPick: (lane: AddLane) => void;
};

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function BarcodeIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M3 5v14M7 5v14M10 5v14M14 5v14M17 5v14M21 5v14" />
    </svg>
  );
}

function CameraIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M4 8h3l2-3h6l2 3h3v11H4z" />
      <circle cx="12" cy="13" r="3.5" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <circle cx="11" cy="11" r="6.5" />
      <path d="M16 16l4.5 4.5" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4" />
    </svg>
  );
}

const ROWS = [
  { lane: "barcode", label: "Scan barcode", Icon: BarcodeIcon },
  { lane: "photo", label: "Take photo", Icon: CameraIcon },
  { lane: "search", label: "Search", Icon: SearchIcon },
  { lane: "voice", label: "Voice log", Icon: MicIcon },
] as const;

export default function AddSheet({ open, onClose, onPick }: Props) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet add-sheet" role="dialog" aria-label="Log food">
        <div className="subsheet-title">Log food</div>
        {ROWS.map(({ lane, label, Icon }) => (
          <button
            key={lane}
            type="button"
            className="add-row"
            onClick={() => onPick(lane)}
          >
            <Icon />
            {label}
          </button>
        ))}
      </div>
    </>
  );
}
