import type { ReactNode } from "react";
import Link from "next/link";
import "../welcome/landing.css";
import "./legal.css";

/** The shell for the public legal pages. A route group, so the URLs stay
 *  /privacy and /terms. The nav reuses the landing page's own classes so the
 *  two cannot drift. Both routes are public — proxy.ts excludes them from the
 *  signed-out redirect, because Oura's OAuth app registration links them. */
export default function LegalLayout({ children }: { children: ReactNode }) {
  return (
    <main className="legal">
      <header className="land-nav">
        <Link href="/" className="land-mark legal-mark">
          Tally
        </Link>
        <nav className="land-nav-links">
          <Link href="/privacy">Privacy</Link>
          <Link href="/terms">Terms</Link>
        </nav>
      </header>
      {children}
    </main>
  );
}
