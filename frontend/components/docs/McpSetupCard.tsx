"use client";

import Link from "next/link";
import { useId, useState, type ReactNode } from "react";
import { CodeBlock } from "./CodeBlock";
import { useRovingTabs } from "./CodeTabs";
import { CLAUDE_CODE_ADD, MCP_JSON, MCP_TS, MCP_URL } from "./snippets";

interface McpClient {
  glyph: string;
  name: string;
  sub: string;
  file: string;
  lang: string;
  code: string;
  hint?: ReactNode;
}

const CLIENTS: McpClient[] = [
  {
    glyph: ">_",
    name: "Claude Code",
    sub: "Run in your terminal",
    file: "terminal",
    lang: "bash",
    code: CLAUDE_CODE_ADD,
  },
  {
    glyph: "{ }",
    name: "JSON clients",
    sub: "Cursor, .mcp.json",
    file: "mcp.json",
    lang: "json",
    code: MCP_JSON,
    hint: (
      <>
        A client that only starts local (stdio) servers, like Claude Desktop&apos;s config file,
        can bridge with <code>npx mcp-remote</code>:{" "}
        <Link href="/docs/mcp#json-clients">config on the MCP page</Link>.
      </>
    ),
  },
  {
    glyph: "TS",
    name: "Your own agent",
    sub: "MCP SDK for TypeScript",
    file: "agent.ts",
    lang: "typescript",
    code: MCP_TS,
    hint: (
      <>
        Python instead? <Link href="/docs/mcp#your-own-agent">The MCP page</Link> has the same
        client in the Python SDK.
      </>
    ),
  },
];

/** "Connect an agent over MCP": pick a client, get its snippet. The tiles are
 *  a tablist (same keyboard model as CodeTabs) and each client's snippet is a
 *  tabpanel; every panel is in the static HTML, inactive ones hidden. */
export function McpSetupCard() {
  const id = useId();
  const [active, setActive] = useState(0);
  const { listRef, onKeyDown } = useRovingTabs(CLIENTS.length, active, setActive);

  return (
    <div className="docs-mcp">
      <div>
        <p className="docs-mcp-title">Set up Tally MCP</p>
        <p className="docs-mcp-desc">
          Give an AI agent your diary: it can read your days and goals, and with a read &amp; write
          key, log food for you.
        </p>
      </div>
      <div
        ref={listRef}
        role="tablist"
        aria-label="MCP client"
        className="docs-mcp-tiles"
        onKeyDown={onKeyDown}
      >
        {CLIENTS.map((c, i) => (
          <button
            key={c.name}
            type="button"
            role="tab"
            id={`${id}-tab-${i}`}
            aria-selected={i === active}
            aria-controls={`${id}-panel-${i}`}
            tabIndex={i === active ? 0 : -1}
            className="docs-mcp-tile"
            onClick={() => setActive(i)}
          >
            <span className="docs-mcp-glyph" aria-hidden="true">
              {c.glyph}
            </span>
            <span className="docs-mcp-tile-text">
              <span className="docs-mcp-name">{c.name}</span>
              <span className="docs-mcp-sub">{c.sub}</span>
            </span>
          </button>
        ))}
      </div>
      {CLIENTS.map((c, i) => (
        <div
          key={c.name}
          role="tabpanel"
          id={`${id}-panel-${i}`}
          aria-labelledby={`${id}-tab-${i}`}
          hidden={i !== active}
          className="docs-mcp-panel"
        >
          <CodeBlock code={c.code} lang={c.lang} title={c.file} />
          {c.hint && <p className="docs-mcp-note">{c.hint}</p>}
        </div>
      ))}
      <p className="docs-mcp-note">
        Using another MCP client? Point it at <code>{MCP_URL}</code> (Streamable HTTP) and send{" "}
        <code>Authorization: Bearer tally_live_…</code> with every request.
      </p>
    </div>
  );
}
