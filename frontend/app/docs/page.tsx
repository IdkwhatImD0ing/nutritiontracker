import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock } from "@/components/docs/CodeBlock";
import { CodeTabs } from "@/components/docs/CodeTabs";
import { DocsPage } from "@/components/docs/DocsPage";
import { McpSetupCard } from "@/components/docs/McpSetupCard";
import {
  COMMIT_RESPONSE,
  DIARY_CURL,
  DIARY_FETCH,
  DIARY_PY,
  DIARY_RESPONSE,
  LOG_FETCH,
  LOG_PY,
  LOG_RESPONSE,
} from "@/components/docs/snippets";
import { SITE_URL } from "@/lib/site";

// The docs layout's title template does not apply to a page in its own
// segment, so this one spells its full title out.
export const metadata: Metadata = {
  title: { absolute: "Introduction · Tally docs" },
  description:
    "Read your Tally diary and log food from your own code or an AI agent: API keys, REST, and MCP.",
};

const TOC = [
  { id: "get-started", label: "Get started" },
  { id: "mcp", label: "Connect an agent over MCP" },
  { id: "first-request", label: "Make your first request" },
  { id: "log-food", label: "Log food" },
  { id: "next", label: "Where next" },
];

export default function DocsIntroduction() {
  return (
    <DocsPage href="/docs" toc={TOC}>
      <h1>
        Your food diary, <span className="accent">from your own code.</span>
      </h1>
      <p className="docs-lede">
        The Tally app runs on a plain HTTP API. A personal API key lets your scripts and AI agents
        call the same endpoints: read your days and goals, and log food the way the app does,
        staged first and saved only when you commit.
      </p>
      <div className="docs-cta">
        <Link href="/settings" className="docs-btn lg">
          Create an API key
        </Link>
        <Link href="/auth/sign-up" className="docs-btn lg ghost">
          Try a demo
        </Link>
      </div>

      <h2 id="get-started">Get started</h2>
      <p>
        Every endpoint lives under <code>{SITE_URL}/api</code>. Authenticate each request with a
        personal key in the <code>Authorization</code> header:{" "}
        <code>Authorization: Bearer tally_live_…</code>. A key looks like{" "}
        <code>tally_live_&lt;keyId&gt;_&lt;secret&gt;</code> and acts as the account that created
        it. Create one in the app under <strong>Settings → API keys</strong>; it is shown once, so
        copy it then. <Link href="/docs/api-keys">More on keys and scopes</Link>.
      </p>
      <p>
        Demo accounts can create keys too. A key carries its account&apos;s limits, so a demo
        key can log once a day, like the app. <Link href="/docs/limits">Limits</Link>.
      </p>

      <h2 id="mcp">Connect an agent over MCP</h2>
      <p>
        Tally runs a Model Context Protocol server at <code>{SITE_URL}/api/mcp</code>. Pick your
        client, paste your key where it says <code>tally_live_...</code>, and your agent can answer
        &ldquo;what did I eat today?&rdquo;. A read-only key gets the read tools only.
      </p>
      <McpSetupCard />

      <h2 id="first-request">Make your first request</h2>
      <p>
        Read today&apos;s diary. With no <code>?date=YYYY-MM-DD</code> it returns today in the
        timezone set in your Tally settings.
      </p>
      <CodeTabs
        label="Request language"
        tabs={[
          { label: "cURL", lang: "bash", code: DIARY_CURL },
          { label: "Python", lang: "python", code: DIARY_PY },
          { label: "fetch", lang: "javascript", code: DIARY_FETCH },
        ]}
      />
      <p>
        The response lists the day&apos;s entries under all four meals (empty ones included) and
        the day&apos;s totals against your targets. Targets read as zero until you set goals.
      </p>
      <CodeBlock code={DIARY_RESPONSE} lang="json" title="200 OK · GET /api/diary" />
      <p className="docs-callout">
        Call the API from a server, a script or an agent, not from a web page: the API sends no
        CORS headers, and a key inside a page is a key anyone can read.
      </p>

      <h2 id="log-food">Log food</h2>
      <p>
        Logging is two calls, stage then commit, the same flow the app uses. First{" "}
        <code>POST /api/logs</code> with what you ate in words. Tally works out the foods,
        portions and meal, looks the foods up, and returns staged rows. Nothing is in your diary
        yet. Then <code>{"POST /api/logs/{log_id}/commit"}</code> with the rows you want to keep.
      </p>
      <ul>
        <li>
          <code>id</code> is a UUID you generate. Sending the same <code>id</code> again returns
          what was stored instead of logging twice, so retries are safe.
        </li>
        <li>
          <code>captured_at</code> and <code>tz</code> (an IANA name such as{" "}
          <code>America/Los_Angeles</code>) decide the meal. Words like &ldquo;yesterday&rdquo;
          move the rows to that day; <code>logged_at</code> in the response says where.
        </li>
        <li>
          The call waits while Tally looks the foods up, which can take up to about two
          minutes. Give your HTTP client a long timeout.
        </li>
      </ul>
      <CodeTabs
        label="Request language"
        tabs={[
          { label: "Python", lang: "python", code: LOG_PY },
          { label: "fetch", lang: "javascript", code: LOG_FETCH },
        ]}
      />
      <p>
        The stage response, trimmed. At commit, only rows with{" "}
        <code>status: &quot;resolved&quot;</code> go into the diary. Any other row you send back
        stays on the log, uncommitted; any row you leave out is removed. When the agent needs to
        ask you something first, <code>items</code> is empty, <code>agent.reply</code> is the
        question and <code>agent.options</code> may list answers.
      </p>
      <CodeBlock code={LOG_RESPONSE} lang="json" title="200 OK · POST /api/logs" />
      <p>
        The commit answers with the day&apos;s new totals. A row the commit refused appears in{" "}
        <code>errors</code> with its <code>item_index</code>; the rest are saved.
      </p>
      <CodeBlock
        code={COMMIT_RESPONSE}
        lang="json"
        title="200 OK · POST /api/logs/{log_id}/commit"
      />

      <h2 id="next">Where next</h2>
      <ul>
        <li>
          <Link href="/docs/api-keys">API keys</Link>: scopes, expiry, revoking.
        </li>
        <li>
          <Link href="/docs/rest">REST API</Link>: the endpoints by area, with the{" "}
          <a href="/api/openapi.json">OpenAPI document</a>.
        </li>
        <li>
          <Link href="/docs/mcp">MCP server</Link>: all fifteen tools and what they do.
        </li>
        <li>
          <Link href="/docs/limits">Limits &amp; errors</Link>: the demo quota and the error
          shapes.
        </li>
      </ul>
    </DocsPage>
  );
}
