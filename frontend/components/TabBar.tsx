"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useState, type MouseEvent } from "react";
import AddSheet, {
  ADD_EVENT,
  ADD_HANDOFF_FROM_KEY,
  ADD_HANDOFF_KEY,
  type AddLane,
} from "./AddSheet";
import { useBehind } from "./BackBar";
import "@/app/library/m3.css";

/** §7.4 navigation: Today · History · [+] · Library · Settings — five equal
 *  slots, so the raised + sits dead centre (owner feedback, 2026-09-06; the
 *  gear used to be a narrow unlabelled sixth thing on the right). The + is
 *  the one way to log (owner ruling, 2026-09-02 — the mid-screen capture bar
 *  is gone): it opens AddSheet, and the pick reaches Today as a window event
 *  while Today is mounted, else as a sessionStorage handoff followed by a
 *  navigation home, with the page it came from: a lane closed without
 *  sending anything goes back there. No props — every page mounts it
 *  as-is; the stylesheet rides along with the import. From 900px m3.css
 *  stands this same markup up as a navigation rail on the left; .rail-only
 *  marks the wordmark and the + button's label, which only the rail shows. */

const stroke = {
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

/* Each tab's glyph has a selected form, not only the accent tint: a filled
   calendar header, filled bars, a filled cover, a filled cog. */
type TabIconProps = { active: boolean };

/** a calendar page with today marked; a clock read as History / Recents */
function TodayIcon({ active }: TabIconProps) {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <rect x="4" y="5" width="16" height="15" rx="2.5" />
      {active ? (
        <path d="M4 10V7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5V10z" fill="currentColor" />
      ) : (
        <path d="M4 10h16" />
      )}
      <path d="M8 3v4M16 3v4" />
      <path d="M12 15h.01" strokeWidth={3} />
    </svg>
  );
}

function HistoryIcon({ active }: TabIconProps) {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M4 19h16" />
      {active ? (
        <path d="M6 10.5h2V16H6zM11 6.5h2V16h-2zM16 12.5h2V16h-2z" fill="currentColor" />
      ) : (
        <path d="M7 16v-5M12 16V7M17 16v-3" />
      )}
    </svg>
  );
}

function LibraryIcon({ active }: TabIconProps) {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      <path d="M5 4h11a2 2 0 0 1 2 2v14H7a2 2 0 0 1-2-2z" />
      {active ? (
        <path d="M5 17V4h11a2 2 0 0 1 2 2v9H7a2 2 0 0 0-2 2z" fill="currentColor" />
      ) : (
        <path d="M5 17a2 2 0 0 1 2-2h11M9 8h5" />
      )}
    </svg>
  );
}

/** eight teeth (radius 9.8, root 7.3); a ring of rays read as brightness */
const COG = "M10.1 4.9L10.5 2.3A9.8 9.8 0 0 1 13.5 2.3L13.9 4.9A7.3 7.3 0 0 1 15.7 5.7L17.8 4.1A9.8 9.8 0 0 1 19.9 6.2L18.3 8.4A7.3 7.3 0 0 1 19.1 10.1L21.7 10.5A9.8 9.8 0 0 1 21.7 13.5L19.1 13.9A7.3 7.3 0 0 1 18.3 15.6L19.9 17.8A9.8 9.8 0 0 1 17.8 19.9L15.7 18.3A7.3 7.3 0 0 1 13.9 19.1L13.5 21.7A9.8 9.8 0 0 1 10.5 21.7L10.1 19.1A7.3 7.3 0 0 1 8.4 18.3L6.2 19.9A9.8 9.8 0 0 1 4.1 17.8L5.7 15.6A7.3 7.3 0 0 1 4.9 13.9L2.3 13.5A9.8 9.8 0 0 1 2.3 10.5L4.9 10.1A7.3 7.3 0 0 1 5.7 8.3L4.1 6.2A9.8 9.8 0 0 1 6.2 4.1L8.3 5.7A7.3 7.3 0 0 1 10.1 4.9z";

function GearIcon({ active }: TabIconProps) {
  return (
    <svg viewBox="0 0 24 24" {...stroke} aria-hidden="true">
      {active ? (
        <path d={`${COG}M9 12a3 3 0 1 0 6 0a3 3 0 1 0-6 0z`} fill="currentColor" fillRule="evenodd" />
      ) : (
        <>
          <path d={COG} />
          <circle cx="12" cy="12" r="3" />
        </>
      )}
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

  // Re-tapping the tab you are on scrolls it to the top, as iOS does. Only
  // on the tab's own page: from a drill-in under it (a day, food detail) the
  // tab still navigates up to its root. A modified click stays the link's.
  const toTop = (href: string) => (e: MouseEvent<HTMLAnchorElement>) => {
    if (pathname !== href || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: still ? "auto" : "smooth" });
  };

  // scroll={false}: each tab root puts its own scroll back (useScrollMemory),
  // which Next's scroll-to-top on a push would undo.
  const tab = ({ href, label, Icon }: (typeof LEFT)[number]) => (
    <Link
      key={href}
      href={href}
      scroll={false}
      className={isActive(href) ? "active" : undefined}
      aria-current={isActive(href) ? "page" : undefined}
      onClick={toTop(href)}
    >
      <Icon active={isActive(href)} />
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
      sessionStorage.setItem(ADD_HANDOFF_FROM_KEY, pathname);
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
          onClick={toTop("/settings")}
        >
          <GearIcon active={isActive("/settings")} />
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
