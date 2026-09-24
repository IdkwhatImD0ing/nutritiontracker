import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock } from "@/components/docs/CodeBlock";
import { DocsPage } from "@/components/docs/DocsPage";

/** Limits from services/quota.py (demo quota), deadline.py + vercel.json
 *  (the request budget) and the error details raised by auth.py and the
 *  route modules. The quoted `detail` strings are the code's own. */

export const metadata: Metadata = {
  title: "Limits & errors",
  description:
    "The demo account quota, the two-minute request budget, and the shape of every Tally API error.",
};

const TOC = [
  { id: "demo", label: "Demo accounts" },
  { id: "time", label: "Request time" },
  { id: "other-limits", label: "Other limits" },
  { id: "errors", label: "Errors" },
  { id: "mcp-errors", label: "Errors over MCP" },
];

const QUOTA_429 = `{
  "detail": "Demo accounts can log once a day — resets at 2026-09-24T00:00:00+00:00",
  "resets_at": "2026-09-24T00:00:00+00:00",
  "used": 1,
  "limit": 1
}`;

const VALIDATION_422 = `{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "text"],
      "msg": "Field required",
      "input": {
        "id": "7c0e5a0e-2f4b-4f7e-9d0a-5b8f1f3c2a91",
        "captured_at": "2026-09-23T15:15:00Z",
        "tz": "UTC"
      }
    }
  ]
}`;

export default function LimitsDoc() {
  return (
    <DocsPage href="/docs/limits" toc={TOC}>
      <h1>Limits &amp; errors</h1>
      <p className="docs-lede">
        What a key can spend, how long a request may take, and what comes back when something is
        refused.
      </p>

      <h2 id="demo">Demo accounts</h2>
      <p>
        Tally has one owner account with no limits. Every other account is a demo account, and a
        key always carries its account&apos;s limits. Demo limits count per UTC day and reset at
        the next UTC midnight.
      </p>
      <div className="docs-table">
        <table>
          <thead>
            <tr>
              <th scope="col">What</th>
              <th scope="col">Demo limit</th>
              <th scope="col">Counts</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Log attempts</td>
              <td>1 a day</td>
              <td>
                <code>POST /api/logs</code>, <code>POST /api/logs/from-food</code>, MCP{" "}
                <code>log_food</code>, and every log made in the app, all together.
              </td>
            </tr>
            <tr>
              <td>Re-resolutions</td>
              <td>10 a day</td>
              <td>
                <code>POST /api/resolve</code>, <code>POST /api/resolve/tier3</code>, a
                free-text <code>POST /api/resolve/answer</code>, and each row MCP{" "}
                <code>retry_lookups</code> looks up.
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <ul>
        <li>
          A retry with the same log <code>id</code> is answered from what was stored and costs
          nothing. Only a retry that has to re-run a log that died part-way counts again.
        </li>
        <li>
          If the agent only asked you a question and staged nothing, the attempt is handed back so
          your answer can use it, at most 3 times a day.
        </li>
        <li>
          During a log, a food Tally has never seen is looked up, and on a demo account that
          lookup also counts against the 10. Past the 10, those rows come back{" "}
          <code>searching</code> instead of failing the log.
        </li>
        <li>
          Reads, commits, entry edits and deletes, weigh-ins, saved meals and settings are not
          counted.
        </li>
      </ul>
      <p>
        <code>GET /api/me</code> (or the MCP tool <code>whoami</code>) shows where you stand:{" "}
        <code>demo</code> is <code>&#123;limit, used_today, resets_at&#125;</code>, or{" "}
        <code>null</code> for the owner. Over the limit, the API answers <code>429</code>:
      </p>
      <CodeBlock code={QUOTA_429} lang="json" title="429 Too Many Requests" />

      <h2 id="time">Request time</h2>
      <p>
        Every request must finish inside the hosting platform&apos;s 120-second limit. Two kinds
        of call can come close. Logging (<code>POST /api/logs</code>, MCP <code>log_food</code>)
        and MCP <code>retry_lookups</code> run on their own 105-second clock, and a row whose
        lookup does not fit comes back <code>searching</code> rather than holding the request
        open. The row fixes (
        <code>POST /api/resolve</code>, <code>POST /api/resolve/tier3</code> and a free-text{" "}
        <code>POST /api/resolve/answer</code>) can run a fresh lookup with no clock of its own,
        which often takes 15 seconds or more and can run up to the 120-second limit. Set your
        HTTP client&apos;s timeout (or your MCP client&apos;s tool timeout) to at least 120 seconds
        for all of these. Everything else answers in seconds.
      </p>
      <p>
        A lost response is safe to retry: send <code>POST /api/logs</code> again with the same{" "}
        <code>id</code> (or call MCP <code>log_food</code> with the same <code>id</code>) and you
        get the stored result instead of a second log. Commits are safe to retry too.
      </p>

      <h2 id="other-limits">Other limits</h2>
      <ul>
        <li>
          At most 10 active API keys per account, and 50 stored in all: creating a key deletes
          the oldest revoked or expired ones past 50 (
          <Link href="/docs/api-keys#expiry">API keys</Link>).
        </li>
        <li>
          <code>GET /api/weights</code> takes <code>days</code> from 1 to 3660; a weigh-in must be
          between 30 and 300 kg.
        </li>
        <li>
          History windows are fixed: <code>3d</code>, <code>w</code>, <code>m</code>,{" "}
          <code>y</code>.
        </li>
        <li>
          There is no separate per-key rate limit today. Tally is a one-person project, so
          please pace bulk jobs.
        </li>
      </ul>

      <h2 id="errors">Errors</h2>
      <p>
        Errors are JSON with a <code>detail</code> field. For most errors it is a short sentence
        from the code; the table quotes some of them.
      </p>
      <div className="docs-table">
        <table>
          <thead>
            <tr>
              <th scope="col">Status</th>
              <th scope="col">Meaning</th>
              <th scope="col">
                Example <code>detail</code>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>401</td>
              <td>
                No usable credentials. Revoked, expired, unknown and malformed keys look the same,
                and their <code>401</code> carries <code>WWW-Authenticate: Bearer</code>.
              </td>
              <td>
                <code>missing bearer token</code>, <code>invalid API key</code>
              </td>
            </tr>
            <tr>
              <td>403</td>
              <td>
                Refused: a read-only key on a write, a key on{" "}
                <code>/api/keys</code>, an unverified email, or a signed-in app request from
                another site (API keys are not checked for <code>Origin</code>).
              </td>
              <td>
                <code>this API key is read-only</code>,{" "}
                <code>API keys cannot manage API keys; sign in to the app</code>,{" "}
                <code>email not verified</code>, <code>origin mismatch</code>
              </td>
            </tr>
            <tr>
              <td>404</td>
              <td>Not found, or it belongs to another account.</td>
              <td>
                <code>log not found</code>, <code>entry not found</code>,{" "}
                <code>meal not found</code>, <code>unknown log item</code>
              </td>
            </tr>
            <tr>
              <td>409</td>
              <td>
                The thing is in the wrong state: the log was already committed or discarded, a row
                was already committed, a name is taken, or the 10-key cap is reached.
              </td>
              <td>
                <code>log already committed</code>, <code>log is discarded</code>,{" "}
                <code>item already committed</code>
              </td>
            </tr>
            <tr>
              <td>422</td>
              <td>
                The request is malformed. Body validation gives a list (below); a route&apos;s own
                check gives a sentence.
              </td>
              <td>
                <code>nothing to change</code>, <code>qty must be positive</code>,{" "}
                <code>empty query</code>
              </td>
            </tr>
            <tr>
              <td>429</td>
              <td>
                Demo quota spent. Also carries <code>resets_at</code>, <code>used</code> and{" "}
                <code>limit</code> (above).
              </td>
              <td>
                <code>Demo accounts can log once a day — resets at …</code>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>A body that fails validation, here a log with no <code>text</code>:</p>
      <CodeBlock code={VALIDATION_422} lang="json" title="422 Unprocessable Content" />

      <h2 id="mcp-errors">Errors over MCP</h2>
      <p>
        Authentication failures are plain HTTP: <code>401</code> with a JSON <code>detail</code>{" "}
        and <code>WWW-Authenticate: Bearer</code>. Once a request is in, a tool that fails returns
        a tool result marked as an error, with a readable message, such as the demo quota and when
        it resets. Your agent sees the message and can tell you.
      </p>
    </DocsPage>
  );
}
