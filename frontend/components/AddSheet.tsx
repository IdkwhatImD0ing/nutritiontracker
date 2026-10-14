"use client";

import { useEffect } from "react";
import { loadDetector } from "./barcodeDetector";
import SheetHeader from "./SheetHeader";
import { SheetFrame, usePresence } from "./useSheetPresence";

/** The + sheet (owner ruling, 2026-09-02): the mid-screen capture bar is
 *  gone; every way to log opens from the tab bar's + button. Exactly four
 *  rows, in this order, and under them a Cancel that is the sheet's exit,
 *  not a fifth lane (Bill, 2026-10-13). The sheet only reports the pick —
 *  TabBar routes it to Today (a window event while Today is mounted, a
 *  sessionStorage handoff plus a navigation home when it isn't), and Today
 *  mounts the lane's sheet. */

export const ADD_LANES = ["barcode", "photo", "search", "agent"] as const;
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
  {
    lane: "barcode",
    label: "Scan barcode",
    sub: "Packaged food, numbers from the label",
    Icon: BarcodeIcon,
  },
  {
    // §3.4b (2026-09-08): the shot opens the Agent chat and lands in it —
    // the plate is read, staged, and then corrected in words
    lane: "photo",
    label: "Take photo",
    sub: "Reads the plate — then tell it what's off",
    Icon: CameraIcon,
  },
  {
    lane: "search",
    label: "Search",
    sub: "Your foods first, then the USDA database",
    Icon: SearchIcon,
  },
  {
    // §6.4 (2026-09-06): the agent's chat window — voice-first (recording
    // starts on open), typing the fallback; it looks things up and shows you
    lane: "agent",
    label: "Agent",
    sub: "Say, type or snap it — it looks things up and fills in the rows",
    Icon: MicIcon,
  },
] as const;

export default function AddSheet({ open, onClose, onPick }: Props) {
  const { mounted, closing } = usePresence(open);

  // Warm the barcode decoder (its ~1 MB wasm) the moment the sheet opens:
  // that download is the slow part of a first scan, and it is one HTTP-cached
  // fetch per page, so a sheet opened for another lane costs it once.
  useEffect(() => {
    if (open) loadDetector().catch(() => {});
  }, [open]);

  if (!mounted) return null;

  return (
    <SheetFrame closing={closing}>
      <div className="subsheet-backdrop" onClick={onClose} />
      <div className="subsheet add-sheet" role="dialog" aria-label="Log food">
        <SheetHeader title="Log food" onClose={onClose} closeButton={false} />
        {ROWS.map(({ lane, label, sub, Icon }) => (
          <button
            key={lane}
            type="button"
            className="add-row"
            onClick={() => onPick(lane)}
          >
            <span className="add-row-icon">
              <Icon />
            </span>
            <span className="add-row-text">
              {label}
              <span className="add-row-sub">{sub}</span>
            </span>
            <span className="add-row-chev" aria-hidden="true">
              ›
            </span>
          </button>
        ))}
        {/* iOS's action-sheet Cancel. It lies where the + was, so a second
            tap on the + cancels rather than opening Agent, whose mic starts
            on open. Hidden from 900px (m3.css), where the rail's Log food is
            nowhere near the tiles. */}
        <button type="button" className="add-cancel" onClick={onClose}>
          Cancel
        </button>
      </div>
    </SheetFrame>
  );
}
