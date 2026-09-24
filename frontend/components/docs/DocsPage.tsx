import type { ReactNode } from "react";
import { DocsPager } from "./DocsPager";
import { DOCS } from "./nav";

export interface TocItem {
  id: string;
  label: string;
}

/** The frame every docs page shares: the group eyebrow (from nav.ts), the
 *  prose column, prev/next, and — on wide screens only — an "On this page"
 *  list of the page's h2s. Pages pass their own `toc` so the ids are written
 *  once, next to the headings they point at. */
export function DocsPage({
  href,
  toc = [],
  children,
}: {
  href: string;
  toc?: TocItem[];
  children: ReactNode;
}) {
  const doc = DOCS.find((d) => d.href === href);
  return (
    <div className="docs-page">
      <div className="docs-main">
        <article className="docs-prose">
          {doc && <p className="docs-eyebrow">{doc.group}</p>}
          {children}
        </article>
        <DocsPager href={href} />
      </div>
      {toc.length > 1 && (
        <aside className="docs-toc" aria-label="On this page">
          <p className="docs-label">On this page</p>
          <ul>
            {toc.map((t) => (
              <li key={t.id}>
                <a href={`#${t.id}`}>{t.label}</a>
              </li>
            ))}
          </ul>
        </aside>
      )}
    </div>
  );
}

const SCOPE_LABEL = {
  read: "read",
  write: "write",
  session: "session only",
  public: "no auth",
} as const;

/** A reference row: method + path + who may call it, then what it is for.
 *  Used by the REST and API-keys pages. `session` marks routes an API key
 *  cannot call (/api/keys); `public` a route that needs no auth (/api/health;
 *  /api/openapi.json is public too). */
export function Endpoint({
  method,
  path,
  scope,
  children,
}: {
  method: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  path: string;
  scope: keyof typeof SCOPE_LABEL;
  children: ReactNode;
}) {
  return (
    <li className="docs-ep">
      <div className="docs-ep-head">
        <span className={`docs-method ${method.toLowerCase()}`}>{method}</span>
        <code className="docs-ep-path">{path}</code>
        <span className={`docs-scope ${scope}`}>{SCOPE_LABEL[scope]}</span>
      </div>
      <div className="docs-ep-body">{children}</div>
    </li>
  );
}
