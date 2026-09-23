"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { DOC_GROUPS, DOCS } from "./nav";

/** The docs sidebar: every page by group, the current one marked. Client
 *  only for usePathname; the pages themselves stay statically rendered. */
export function DocsNav() {
  const pathname = usePathname();
  return (
    <nav aria-label="Documentation" className="docs-nav">
      {DOC_GROUPS.map((group) => (
        <div key={group} className="docs-nav-group">
          <p className="docs-label">{group}</p>
          <ul>
            {DOCS.filter((d) => d.group === group).map((d) => {
              const active = pathname === d.href;
              return (
                <li key={d.href}>
                  <Link
                    href={d.href}
                    aria-current={active ? "page" : undefined}
                    className={active ? "active" : undefined}
                  >
                    {d.title}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

/** The phone's version: the same nav folded into "Browse docs". Keyed by the
 *  path, so a client-side navigation remounts the <details> and it closes
 *  instead of covering the page the reader just picked. */
export function DocsMobileNav() {
  const pathname = usePathname();
  return (
    <details key={pathname} className="docs-browse">
      <summary>Browse docs</summary>
      <DocsNav />
    </details>
  );
}
