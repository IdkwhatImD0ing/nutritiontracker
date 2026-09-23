import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { DocsMobileNav, DocsNav } from "@/components/docs/DocsNav";
import TallyMark from "@/components/TallyMark";
import { GITHUB_URL } from "@/lib/site";
import "./docs.css";

/** The public developer docs (API keys, REST, MCP). proxy.ts excludes /docs
 *  from the sign-in redirect, and nothing here reads the session, so every
 *  page is prerendered at build time. That is also why the header says "Open
 *  app" rather than guessing Sign in vs. Today: `/` itself decides. */

export const metadata: Metadata = {
  title: { default: "Tally docs", template: "%s · Tally docs" },
  description:
    "Use Tally from your own code: personal API keys, the REST API, and an MCP server for AI agents.",
  // Replaces the root openGraph/twitter (Next overwrites these objects, not
  // merges them). No title, description or url here, so Next fills og/twitter
  // from each docs page and og:url no longer points at the homepage.
  openGraph: {
    siteName: "Tally",
    type: "website",
    locale: "en_US",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        type: "image/png",
        alt: "Tally — say what you ate.",
      },
    ],
  },
  twitter: { card: "summary_large_image", images: ["/og.png"] },
};

export default function DocsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="docs">
      <header className="docs-header">
        <div className="docs-header-in">
          <Link href="/" className="docs-mark" aria-label="Tally home">
            <TallyMark className="docs-mark-icon" />
            Tally
          </Link>
          <Link href="/docs" className="docs-header-label">
            Docs
          </Link>
          <nav className="docs-header-links" aria-label="Site">
            <a href={GITHUB_URL} className="docs-btn ghost">
              GitHub
            </a>
            <Link href="/" className="docs-btn">
              Open app
            </Link>
          </nav>
        </div>
      </header>
      <div className="docs-body">
        <aside className="docs-side">
          <div className="docs-side-mobile">
            <DocsMobileNav />
          </div>
          <div className="docs-side-desktop">
            <DocsNav />
          </div>
        </aside>
        <main className="docs-content">{children}</main>
      </div>
    </div>
  );
}
