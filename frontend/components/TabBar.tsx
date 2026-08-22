"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import "@/app/library/m3.css";

/** §7.4 navigation: a three-tab bar (Today · History · Library) with Settings
 *  behind an icon. No props — every page mounts it as-is; the stylesheet
 *  rides along with the import so Today/History need nothing else. */

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

const TABS = [
  { href: "/", label: "Today", Icon: TodayIcon },
  { href: "/history", label: "History", Icon: HistoryIcon },
  { href: "/library", label: "Library", Icon: LibraryIcon },
];

export default function TabBar() {
  const pathname = usePathname() ?? "/";
  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <nav className="tabbar" aria-label="Main">
      {TABS.map(({ href, label, Icon }) => (
        <Link
          key={href}
          href={href}
          className={isActive(href) ? "active" : undefined}
          aria-current={isActive(href) ? "page" : undefined}
        >
          <Icon />
          {label}
        </Link>
      ))}
      <Link
        href="/settings"
        className={`gear${isActive("/settings") ? " active" : ""}`}
        aria-label="Settings"
        aria-current={isActive("/settings") ? "page" : undefined}
      >
        <GearIcon />
      </Link>
    </nav>
  );
}
