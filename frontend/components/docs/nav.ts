/** The public docs at /docs, in reading order. The sidebar (DocsNav), the
 *  page eyebrows (DocsPage) and the previous/next links (DocsPager) all read
 *  this one list, so adding a page is one entry here plus its page.tsx. */

export type DocGroup = "Get started" | "Reference" | "Operate";

export interface DocEntry {
  href: string;
  title: string;
  group: DocGroup;
}

export const DOC_GROUPS: DocGroup[] = ["Get started", "Reference", "Operate"];

export const DOCS: DocEntry[] = [
  { href: "/docs", title: "Introduction", group: "Get started" },
  { href: "/docs/api-keys", title: "API keys", group: "Reference" },
  { href: "/docs/rest", title: "REST API", group: "Reference" },
  { href: "/docs/mcp", title: "MCP server", group: "Reference" },
  { href: "/docs/limits", title: "Limits & errors", group: "Operate" },
  { href: "/docs/security", title: "Security", group: "Operate" },
];
