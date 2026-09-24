import Link from "next/link";
import { DOCS } from "./nav";

/** Previous / next page at the foot of every doc, in nav.ts order. */
export function DocsPager({ href }: { href: string }) {
  const i = DOCS.findIndex((d) => d.href === href);
  const prev = i > 0 ? DOCS[i - 1] : null;
  const next = i >= 0 && i < DOCS.length - 1 ? DOCS[i + 1] : null;
  if (!prev && !next) return null;
  return (
    <nav aria-label="Previous and next page" className="docs-pager">
      {prev && (
        <Link href={prev.href} className="docs-pager-link">
          <span className="docs-label">Previous</span>
          <span className="docs-pager-title">← {prev.title}</span>
        </Link>
      )}
      {next && (
        <Link href={next.href} className="docs-pager-link next">
          <span className="docs-label">Next</span>
          <span className="docs-pager-title">{next.title} →</span>
        </Link>
      )}
    </nav>
  );
}
