"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState } from "react";
import AddSheet, { ADD_EVENT, ADD_HANDOFF_KEY, type AddLane } from "./AddSheet";
import { useBehind } from "./BackBar";
import "@/app/library/m3.css";

/** §7.4 navigation: Today · History · [+] · Library · Settings — five equal
 *  slots, so the raised + sits dead centre (owner feedback, 2026-09-06; the
 *  gear used to be a narrow unlabelled sixth thing on the right). The + is
 *  the one way to log (owner ruling, 2026-09-02 — the mid-screen capture bar
 *  is gone): it opens AddSheet, and the pick reaches Today as a window event
 *  while Today is mounted, else as a sessionStorage handoff followed by a
 *  navigation home. No props — every page mounts it as-is; the stylesheet
 *  rides along with the import. From 900px m3.css stands this same markup
 *  up as a navigation rail on the left; .rail-only marks the wordmark and
 *  the + button's label, which only the rail shows. */

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

/** The tab a page belongs to, which is not always the one its URL sits
 *  under: food detail is a drill-in from Library or any diary row (§7.4) and
 *  keeps the tab it was opened from lit, and Experiment is pushed from
 *  Settings. `behind` is the page under this one (BackBar's trail). */
function ownerOf(pathname: string, behind: string | undefined): string {
  if (pathname.startsWith("/library/food/")) {
    if (behind === "/") return "/";
    if (behind?.startsWith("/history")) return "/history";
    return "/library";
  }
  if (pathname.startsWith("/experiment")) return "/settings";
  return pathname;
}

export default function TabBar() {
  const pathname = usePathname() ?? "/";
  const owner = ownerOf(pathname, useBehind());
  const router = useRouter();
  const [addOpen, setAddOpen] = useState(false);
  const isActive = (href: string) =>
    href === "/" ? owner === "/" : owner.startsWith(href);

  // scroll={false}: each tab root puts its own scroll back (useScrollMemory),
  // which Next's scroll-to-top on a push would undo.
  const tab = ({ href, label, Icon }: (typeof LEFT)[number]) => (
    <Link
      key={href}
      href={href}
      scroll={false}
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
        <span className="rail-brand rail-only">Tally</span>
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
            <span className="rail-only">Log food</span>
          </button>
        </div>
        {RIGHT.map(tab)}
        <Link
          href="/settings"
          scroll={false}
          className={`gear${isActive("/settings") ? " active" : ""}`}
          aria-label="Settings"
          aria-current={isActive("/settings") ? "page" : undefined}
        >
          <GearIcon />
          Settings
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
