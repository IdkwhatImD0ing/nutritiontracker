"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

/* The pages behind the one on screen, oldest first, as this document saw
   them. The browser has no answer to "is the entry behind this one ours?":
   history.length also counts other sites' entries and forward ones. And a
   cold launch (a deep link, or iOS reopening the installed app where it was
   left) has nothing of ours behind it, so there back must open the parent
   rather than leave the app, or do nothing at all in standalone mode. A
   pathname change pushes; a popstate (router.back(), a back gesture) back
   onto the page behind pops, and a forward one onto a page stepped back over
   pushes it again. Module state lasts as long as the document, so after a
   reload the trail starts empty and back opens the parent. */
let trail: string[] = [];
/** Pages stepped back over, most recent last: where a forward pop lands. */
let ahead: string[] = [];
let popTo: string | null = null;

/** The trail (and what lies ahead) once `path` is on screen. Pure, so a page
 *  rendering ahead of NavTrail's effect sees the navigation still settling. */
function settle(path: string): { trail: string[]; ahead: string[] } {
  if (trail.at(-1) === path) return { trail, ahead };
  if (popTo !== path) return { trail: [...trail, path], ahead: [] };
  // A popstate does not say which way it went, only where it landed.
  const back = trail.at(-2) === path;
  const forward = ahead.at(-1) === path;
  if (back && !forward) return { trail: trail.slice(0, -1), ahead: [...ahead, trail.at(-1)!] };
  if (forward && !back) return { trail: [...trail, path], ahead: ahead.slice(0, -1) };
  // the same path both behind and ahead could be either step; a pop to
  // neither went past the start of the trail. Nothing known is behind.
  return { trail: [path], ahead: [] };
}

/** Mounted once, in the root layout, so it sees every navigation. */
export function NavTrail() {
  const pathname = usePathname();
  useEffect(() => {
    // during popstate, location already names the entry being restored
    const onPop = () => {
      popTo = location.pathname;
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  useEffect(() => {
    ({ trail, ahead } = settle(pathname));
    popTo = null;
  }, [pathname]);
  return null;
}

const TITLES: Record<string, string> = {
  "/": "Today",
  "/history": "History",
  "/library": "Library",
  "/settings": "Settings",
};

/** The back control of a pushed screen, first in its `.brand` header. It
 *  names and returns to the page actually behind it (food detail and the
 *  day view have more than one way in), else opens `parent`. */
export default function BackBar({ parent, label }: { parent: string; label: string }) {
  const router = useRouter();
  const behind = settle(usePathname()).trail.at(-2);
  const name = behind ? TITLES[behind] : label;
  return (
    <Link
      href={parent}
      className="nav-back"
      aria-label={name ? `Back to ${name}` : undefined}
      onClick={(e) => {
        // a modified click is the link's: the parent in a new tab or window
        if (!behind || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        e.preventDefault();
        router.back();
      }}
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M15 5l-7 7 7 7" />
      </svg>
      {name ?? "Back"}
    </Link>
  );
}
