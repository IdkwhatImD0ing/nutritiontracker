"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import AddSheet, { ADD_EVENT, ADD_HANDOFF_KEY, type AddLane } from "./AddSheet";
import "@/app/library/m3.css";

/** §7.4 navigation: Today · History · [+] · Library, with Settings behind an
 *  icon. The raised + in the middle is the one way to log (owner ruling,
 *  2026-09-02 — the mid-screen capture bar is gone): it opens AddSheet, and
 *  the pick reaches Today as a window event while Today is mounted, else as
 *  a sessionStorage handoff followed by a navigation home. No props — every
 *  page mounts it as-is; the stylesheet rides along with the import. */

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function TodayIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 8v4l2.5 2.5" />
    </svg>
  );
}

function HistoryIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M4 19h16" />
      <path d="M7 16v-5M12 16V7M17 16v-3" />
    </svg>
  );
}

function LibraryIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M5 4h11a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2z" />
      <path d="M5 17a2 2 0 0 1 2-2h11M9 8h5" />
    </svg>
  );
}

function GearIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 3v2.5M12 18.5V21M3 12h2.5M18.5 12H21M5.6 5.6l1.8 1.8M16.6 16.6l1.8 1.8M5.6 18.4l1.8-1.8M16.6 7.4l1.8-1.8" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" {...stroke} strokeWidth={2.4} aria-hidden="true">
      <path d="M12 5v14M5 12h14" />
    </svg>
  );
}

const LEFT = [
  { href: "/", label: "Today", Icon: TodayIcon },
  { href: "/history", label: "History", Icon: HistoryIcon },
];
const RIGHT = [{ href: "/library", label: "Library", Icon: LibraryIcon }];

export default function TabBar() {
  const pathname = usePathname() ?? "/";
  const router = useRouter();
  const [addOpen, setAddOpen] = useState(false);
  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  const tab = ({ href, label, Icon }: (typeof LEFT)[number]) => (
    <Link
      key={href}
      href={href}
      className={isActive(href) ? "active" : undefined}
      aria-current={isActive(href) ? "page" : undefined}
    >
      <Icon />
      {label}
    </Link>
  );

  const pick = (lane: AddLane) => {
    setAddOpen(false);
    if (pathname === "/") {
      window.dispatchEvent(
        new CustomEvent<AddLane>(ADD_EVENT, { detail: lane }),
      );
      return;
    }
    try {
      sessionStorage.setItem(ADD_HANDOFF_KEY, lane);
    } catch {
      /* storage unavailable — Today opens and the + is one tap away again */
    }
    router.push("/");
  };

  return (
    <>
      <nav className="tabbar" aria-label="Main">
        {LEFT.map(tab)}
        <div className="fab-slot">
          <button
            type="button"
            className="fab"
            aria-label="Log food"
            aria-haspopup="dialog"
            aria-expanded={addOpen}
            onClick={() => setAddOpen(true)}
          >
            <PlusIcon />
          </button>
        </div>
        {RIGHT.map(tab)}
        <Link
          href="/settings"
          className={`gear${isActive("/settings") ? " active" : ""}`}
          aria-label="Settings"
          aria-current={isActive("/settings") ? "page" : undefined}
        >
          <GearIcon />
        </Link>
      </nav>
      <AddSheet
        open={addOpen}
        onClose={() => setAddOpen(false)}
        onPick={pick}
      />
    </>
  );
}
