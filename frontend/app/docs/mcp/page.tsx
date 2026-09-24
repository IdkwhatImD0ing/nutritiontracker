import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";
import { CodeBlock } from "@/components/docs/CodeBlock";
import { CodeTabs } from "@/components/docs/CodeTabs";
import { DocsPage } from "@/components/docs/DocsPage";
import {
  CLAUDE_CODE_ADD,
  MCP_JSON,
  MCP_PY,
  MCP_REMOTE_JSON,
  MCP_TS,
  MCP_URL,
} from "@/components/docs/snippets";

/** The MCP server's public contract: endpoint, auth, and the sixteen tools
 *  (names and arguments final per SPEC §2; backend/mcp_app.py implements
 *  them). A tool renamed there must be renamed here. */

export const metadata: Metadata = {
  title: "MCP server",
  description:
    "Connect Claude Code, Cursor, Claude Desktop or your own agent to Tally over the Model Context Protocol.",
};

const TOC = [
  { id: "endpoint", label: "Endpoint and auth" },
  { id: "claude-code", label: "Claude Code" },
  { id: "json-clients", label: "JSON clients" },
  { id: "your-own-agent", label: "Your own agent" },
  { id: "read-tools", label: "Read tools" },
  { id: "write-tools", label: "Write tools" },
  { id: "logging", label: "How logging works" },
  { id: "prompts", label: "Example prompts" },
];

function Tool({
  name,
  args,
  scope,
  note,
  children,
}: {
  name: string;
  args: string;
  scope: "read" | "write";
  note?: string;
  children: ReactNode;
}) {
  return (
    <li className="docs-ep">
      <div className="docs-ep-head">
        <code className="docs-ep-path">
          {name}({args})
        </code>
        <span className={`docs-scope ${scope}`}>{note ? `${scope} · ${note}` : scope}</span>
      </div>
      <div className="docs-ep-body">{children}</div>
    </li>
  );
}

export default function McpDoc() {
  return (
    <DocsPage href="/docs/mcp" toc={TOC}>
      <h1>MCP server</h1>
      <p className="docs-lede">
        Tally speaks the Model Context Protocol, so an AI agent can read your diary and log food
        for you. It uses the same API keys as the REST API and the same rules.
      </p>

      <h2 id="endpoint">Endpoint and auth</h2>
      <ul>
        <li>
          URL: <code>{MCP_URL}</code>, Streamable HTTP transport. Requests are POSTs; GET and
          DELETE answer <code>405</code>. The server is stateless, so there is no session to keep
          alive.
        </li>
        <li>
          Auth: <code>Authorization: Bearer tally_live_…</code> on every request. A missing or
          bad key gets HTTP <code>401</code> with a JSON <code>detail</code> and{" "}
          <code>WWW-Authenticate: Bearer</code>.
        </li>
        <li>
          The key decides the tools. A <strong>read only</strong> key sees the 9 read tools; a{" "}
          <strong>read &amp; write</strong> key sees all 16. A tool your key does not have is not
          listed and cannot be called.
        </li>
        <li>
          Tools act as the key&apos;s account, with its limits: on a demo account,{" "}
          <code>log_food</code> can run once a day (<Link href="/docs/limits">limits</Link>).
        </li>
      </ul>

      <h2 id="claude-code">Claude Code</h2>
      <p>Run this once, with your key in place of the placeholder:</p>
      <CodeBlock code={CLAUDE_CODE_ADD} lang="bash" title="terminal" />

      <h2 id="json-clients">JSON clients</h2>
      <p>
        Clients configured by an <code>mcpServers</code> JSON file take a remote server as a URL
        plus headers. The file&apos;s name and place depend on the client: Claude Code reads{" "}
        <code>.mcp.json</code> in your project, Cursor reads <code>.cursor/mcp.json</code> or{" "}
        <code>~/.cursor/mcp.json</code>.
      </p>
      <CodeBlock code={MCP_JSON} lang="json" title="mcp.json" />
      <p>
        Some clients only start local (stdio) servers; Claude Desktop&apos;s config file is one.
        For those,{" "}
        <a href="https://github.com/geelen/mcp-remote">
          <code>mcp-remote</code>
        </a>{" "}
        runs locally and forwards to Tally. The header is written without a space and passed
        through <code>env</code> because some clients split arguments on spaces:
      </p>
      <CodeBlock code={MCP_REMOTE_JSON} lang="json" title="claude_desktop_config.json" />

      <h2 id="your-own-agent">Your own agent</h2>
      <p>
        Use an MCP SDK&apos;s Streamable HTTP client and pass the header with every request. These
        list the tools and read today&apos;s diary.
      </p>
      <CodeTabs
        label="SDK language"
        tabs={[
          { label: "TypeScript", lang: "typescript", code: MCP_TS },
          { label: "Python", lang: "python", code: MCP_PY },
        ]}
      />
      <p>
        <code>log_food</code> and <code>retry_lookups</code> wait for Tally&apos;s lookups and can
        take up to about two minutes. Give tool calls a longer timeout than that: the TypeScript
        SDK stops waiting after 60 seconds unless you pass <code>timeout</code>.
      </p>

      <h2 id="read-tools">Read tools</h2>
      <p>
        Every key has these. They are marked read-only for clients that show it. Arguments with a{" "}
        <code>?</code> are optional; dates are <code>YYYY-MM-DD</code> in your Tally timezone.
      </p>
      <ul className="docs-eps">
        <Tool name="whoami" args="" scope="read">
          <p>Your email, whether you are the owner, and today&apos;s demo quota.</p>
        </Tool>
        <Tool name="get_day" args="date?" scope="read">
          <p>
            One day&apos;s entries by meal and the day&apos;s totals against your targets. Default:
            today.
          </p>
        </Tool>
        <Tool name="get_history" args={'range = "w"'} scope="read">
          <p>
            Daily totals, targets and your logging streak. <code>range</code> is{" "}
            <code>&quot;3d&quot;</code>, <code>&quot;w&quot;</code> (7 days),{" "}
            <code>&quot;m&quot;</code> (30 days) or <code>&quot;y&quot;</code> (52 weeks, as weekly
            averages).
          </p>
        </Tool>
        <Tool name="get_goals" args="" scope="read">
          <p>
            Your targets (<code>null</code> when none are set), body profile and timezone.
          </p>
        </Tool>
        <Tool name="get_weights" args="days = 90" scope="read">
          <p>Your latest weigh-in and the weigh-ins of the last <code>days</code> days.</p>
        </Tool>
        <Tool name="list_saved_meals" args="include_inactive = false" scope="read">
          <p>Your saved meals and what is in them.</p>
        </Tool>
        <Tool name="search_my_foods" args="query?, limit = 20" scope="read">
          <p>
            Foods you have logged before, each with the amount you last had. Without a{" "}
            <code>query</code>, the most recent ones.
          </p>
        </Tool>
        <Tool name="search_foods" args="query, include_usda = false" scope="read">
          <p>
            Searches Tally&apos;s food cache. With <code>include_usda</code> it also searches USDA
            FoodData Central.
          </p>
        </Tool>
        <Tool name="get_food" args="food_id" scope="read">
          <p>One food&apos;s servings and nutrients, without the raw provider record.</p>
        </Tool>
      </ul>

      <h2 id="write-tools">Write tools</h2>
      <p>Only a read &amp; write key has these.</p>
      <ul className="docs-eps">
        <Tool name="log_food" args="text, tz?, id?" scope="write">
          <p>
            Stages a log from what you ate, in words, and returns its <code>log_id</code>, the
            meal, and each row: index, status, food, amount, serving, macros, alternates, and any
            question. Also the agent&apos;s reply and options. <strong>Rows are staged, not
            saved</strong>: call <code>commit_log</code> to save them. <code>tz</code> defaults to
            the timezone in your settings.
          </p>
          <p>
            <code>id</code> is any UUID you pick. If a call times out, call again with the same{" "}
            <code>id</code>: you get the rows the first call has stored so far instead of a second
            log. That answer has no reply or options, and while the first call is still running
            its rows may be incomplete, so try again shortly. A row left <code>searching</code> ran
            out of time for its lookup; <code>retry_lookups</code> runs it again.
          </p>
        </Tool>
        <Tool name="commit_log" args="log_id, drop_items?, save_as_meal?" scope="write">
          <p>
            Saves a staged log&apos;s rows to the diary. <code>drop_items</code> lists row indexes
            to leave out; <code>save_as_meal</code> also saves the rows as a saved meal under that
            name.
          </p>
        </Tool>
        <Tool name="retry_lookups" args="log_id" scope="write">
          <p>
            Looks up again, one after another, each row of a log that is still{" "}
            <code>searching</code>, and returns the log&apos;s rows in <code>log_food</code>&apos;s
            shape. On a staged log, <code>commit_log</code> then saves the rows that resolved. On a
            log already committed, this call saves each row it resolves to the diary itself. The
            rows share one request&apos;s time, up to about two minutes; a row there was no time
            for stays <code>searching</code>, so call again. A discarded log is refused. On a demo
            account each lookup counts toward the 10 a day (
            <Link href="/docs/limits#demo">limits</Link>).
          </p>
        </Tool>
        <Tool name="discard_log" args="log_id" scope="write">
          <p>Throws a staged log away.</p>
        </Tool>
        <Tool name="edit_entry" args="entry_id, qty?, serving_id?, food_id?" scope="write">
          <p>Changes a diary entry&apos;s amount, serving or food.</p>
        </Tool>
        <Tool name="delete_entry" args="entry_id" scope="write" note="destructive">
          <p>
            Deletes a diary entry for good. It is marked destructive, so your client can ask
            you before it runs.
          </p>
        </Tool>
        <Tool name="log_weight" args={'weight, unit: "kg" | "lb", day?'} scope="write">
          <p>Records a weigh-in; one per day, so a second one replaces the first.</p>
        </Tool>
      </ul>

      <h2 id="logging">How logging works</h2>
      <p>
        Logging over MCP is the app&apos;s own flow. <code>log_food</code> runs Tally&apos;s
        logging agent on your words. The agent names the foods and amounts; the calories and
        macros come from a food record, such as USDA FoodData Central or a restaurant&apos;s own
        published nutrition page, cited with a link. A model never makes the numbers up, and a
        food Tally cannot find comes back <code>not_found</code> instead of a guess.
      </p>
      <p>
        Nothing is in your diary until <code>commit_log</code>. That leaves room for your agent
        to show you the rows and ask before saving. Only rows with status{" "}
        <code>resolved</code> are saved; a row still <code>searching</code> or waiting on a
        question is left uncommitted. <code>retry_lookups</code> looks a <code>searching</code> row
        up again, before the commit or after it. Photos are not available over MCP.
      </p>

      <h2 id="prompts">Example prompts</h2>
      <ul>
        <li>&ldquo;What have I eaten today, and how much protein do I have left?&rdquo;</li>
        <li>&ldquo;How did this week go against my calorie target?&rdquo;</li>
        <li>
          &ldquo;Log two eggs and a fairlife for breakfast. Show me the rows before you save
          them.&rdquo;
        </li>
        <li>&ldquo;I weighed 180.4 lb this morning.&rdquo;</li>
        <li>&ldquo;Change the rice at lunch to one cup.&rdquo;</li>
      </ul>
    </DocsPage>
  );
}
