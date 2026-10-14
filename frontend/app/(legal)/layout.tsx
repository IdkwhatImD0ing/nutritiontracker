import type { ReactNode } from "react";
import Link from "next/link";
import TallyMark from "@/components/TallyMark";
import "../welcome/landing.css";
import "./legal.css";

/** The shell for the public legal pages. A route group, so the URLs stay
 *  /privacy and /terms. The header is the landing page's own markup and
 *  classes, so the two cannot drift — `.land` included, which defines the
 *  gutter `.land-wrap` pads with. Both routes are public — proxy.ts excludes
 *  them from the signed-out redirect, because Oura's OAuth app registration
 *  links them. */
export default function LegalLayout({ children }: { children: ReactNode }) {
  return (
    <div className="land">
      <header className="land-nav">
        <div className="land-wrap land-nav-in">
          <Link href="/" className="land-mark">
            <TallyMark className="land-mark-icon" />
            <span>Tally</span>
          </Link>
          <nav className="land-nav-links" aria-label="Legal">
            <Link href="/privacy">Privacy</Link>
            <Link href="/terms">Terms</Link>
          </nav>
        </div>
      </header>
      <main className="legal">{children}</main>
    </div>
  );
}
