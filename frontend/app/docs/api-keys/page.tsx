import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock } from "@/components/docs/CodeBlock";
import { DocsPage, Endpoint } from "@/components/docs/DocsPage";
import { SITE_URL } from "@/lib/site";

/** Mirrors backend/api_keys.py (format, hashing, redaction) and
 *  routes/keys.py + auth.py (scopes, expiry, the 10-key cap, revocation,
 *  session-only management) — SPEC §1. Change one, change the other. */

export const metadata: Metadata = {
  title: "API keys",
  description:
    "Create, scope, expire and revoke personal Tally API keys, and how the key format works.",
};

const TOC = [
  { id: "create", label: "Create a key" },
  { id: "use", label: "Use a key" },
  { id: "format", label: "Key format" },
  { id: "scopes", label: "Scopes" },
  { id: "expiry", label: "Expiry and the 10-key cap" },
  { id: "revoke", label: "Revoke a key" },
  { id: "demo", label: "Demo accounts" },
  { id: "manage", label: "The /api/keys endpoints" },
];

const USE_CURL = `curl ${SITE_URL}/api/me -H "Authorization: Bearer $TALLY_API_KEY"`;

const ME_RESPONSE = `{
  "email": "you@example.com",
  "is_owner": false,
  "demo": { "limit": 1, "used_today": 0, "resets_at": "2026-09-24T00:00:00+00:00" }
}`;

const KEY_OUT = `{
  "id": "3f9c2a7b1d4e6f80",
  "label": "Claude Code on my laptop",
  "scopes": ["read", "write"],
  "redacted": "tally_live_3f9c2a7b1d4e6f80_…a41c",
  "created_at": "2026-09-23T17:02:11Z",
  "last_used_at": null,
  "expires_at": "2026-12-22T17:02:11Z",
  "revoked_at": null
}`;

export default function ApiKeysDoc() {
  return (
    <DocsPage href="/docs/api-keys" toc={TOC}>
      <h1>API keys</h1>
      <p className="docs-lede">
        A personal API key lets a script or an AI agent act as you. It works for the REST API and
        the MCP server alike. Create one in the app, send it as a bearer token, revoke it when you
        are done with it.
      </p>

      <h2 id="create">Create a key</h2>
      <ol>
        <li>
          Sign in to Tally and open <Link href="/settings">Settings → API keys</Link>. Demo
          accounts can create keys too.
        </li>
        <li>
          Choose <strong>New key</strong>. Give it a label (1 to 80 characters) that says where
          it will live, pick <strong>Read only</strong> or <strong>Read &amp; write</strong>, and
          an expiry: never, 30, 90 or 365 days.
        </li>
        <li>
          Copy the key from the banner. <strong>It is shown once.</strong> Tally keeps only a
          hash of it, so it cannot show it again. If you lose it, revoke it and create another.
        </li>
      </ol>

      <h2 id="use">Use a key</h2>
      <p>
        Send it in the <code>Authorization</code> header of every request, REST or MCP. Keep it in
        an environment variable or your agent&apos;s secret store, never in source control or a
        web page.
      </p>
      <CodeBlock code={USE_CURL} lang="bash" />
      <p>
        <code>GET /api/me</code> is a cheap way to check a key: it says whose it is and, for a
        demo account, today&apos;s quota (<code>demo</code> is <code>null</code> for the owner).
      </p>
      <CodeBlock code={ME_RESPONSE} lang="json" title="200 OK · GET /api/me" />

      <h2 id="format">Key format</h2>
      <p>
        <code>tally_live_&lt;keyId&gt;_&lt;secret&gt;</code>, all lowercase:
      </p>
      <ul>
        <li>
          <code>keyId</code> is 16 hex characters (8 random bytes). It names the key and is safe
          to show; the key list uses it.
        </li>
        <li>
          <code>secret</code> is 64 hex characters (32 random bytes, 256 bits).
        </li>
      </ul>
      <p>
        As a regular expression: <code>{"^tally_live_([0-9a-f]{16})_([0-9a-f]{64})$"}</code>.
        The fixed prefix makes a pasted key easy to recognise and to search for. Lists show a
        redacted form, <code>tally_live_&lt;keyId&gt;_…&lt;last 4&gt;</code>, which is enough to
        recognise a key and useless for calling the API.
      </p>

      <h2 id="scopes">Scopes</h2>
      <p>A key has one of two access levels. Write always includes read.</p>
      <div className="docs-table">
        <table>
          <thead>
            <tr>
              <th scope="col">Access</th>
              <th scope="col">
                <code>scopes</code>
              </th>
              <th scope="col">REST</th>
              <th scope="col">MCP</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>Read only</td>
              <td>
                <code>[&quot;read&quot;]</code>
              </td>
              <td>GET, HEAD and OPTIONS</td>
              <td>The 9 read tools</td>
            </tr>
            <tr>
              <td>Read &amp; write</td>
              <td>
                <code>[&quot;read&quot;, &quot;write&quot;]</code>
              </td>
              <td>Every method</td>
              <td>All 15 tools</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p>
        The REST check is by HTTP method. A read-only key on POST, PUT, PATCH or DELETE gets{" "}
        <code>403</code> with <code>{'{"detail": "this API key is read-only"}'}</code>, even
        where the call saves nothing (<code>POST /api/settings/goals/compute</code> is a POST).
        Over MCP, a read-only key is served a tool list without the write tools, so an agent
        never sees them. A signed-in session in the app has both scopes.
      </p>

      <h2 id="expiry">Expiry and the 10-key cap</h2>
      <p>
        A key can expire after 30, 90 or 365 days, or never. After that it stops working like a
        revoked key. Short expiries suit agents you are trying out.
      </p>
      <p>
        An account can hold at most <strong>10 active keys</strong> (not revoked and not yet
        expired). Creating an eleventh answers <code>409</code>; revoke one first.
      </p>
      <p>
        Each key&apos;s <strong>last used</strong> time is updated at most once every five
        minutes, so a busy agent does not write on every call. Treat it as accurate to about five
        minutes.
      </p>

      <h2 id="revoke">Revoke a key</h2>
      <p>
        In <strong>Settings → API keys</strong>, choose <strong>Revoke</strong> and confirm. The
        key stops working on its next request; nothing caches it. The row stays in the list,
        dimmed, so you can still see what it was and when it was last used.
      </p>
      <p>
        A revoked, expired, unknown or malformed key always gets the same answer, so it never
        reveals which keys exist: <code>401</code> with{" "}
        <code>{'{"detail": "invalid API key"}'}</code>.
      </p>

      <h2 id="demo">Demo accounts</h2>
      <p>
        A key acts as its account, so it carries the account&apos;s limits. A demo account&apos;s
        key can log once a day, and that one attempt is shared with the app: a log made through
        the key uses up the day&apos;s log in the app too. The owner account has no quota.{" "}
        <Link href="/docs/limits">Limits &amp; errors</Link> has the details.
      </p>

      <h2 id="manage">The /api/keys endpoints</h2>
      <p>
        The Settings screen uses these. They accept a signed-in app session only: an API key
        calling them gets <code>403</code> with{" "}
        <code>{'{"detail": "API keys cannot manage API keys; sign in to the app"}'}</code>.
        That way a leaked key can never mint a new key or outlive its own revocation.
      </p>
      <ul className="docs-eps">
        <Endpoint method="GET" path="/api/keys" scope="session">
          <p>Your keys, newest first, revoked ones included.</p>
        </Endpoint>
        <Endpoint method="POST" path="/api/keys" scope="session">
          <p>
            Body: <code>label</code> (1 to 80 characters, trimmed), <code>scopes</code>{" "}
            (<code>&quot;read&quot;</code> or <code>&quot;read_write&quot;</code>, default{" "}
            <code>&quot;read_write&quot;</code>), <code>expires_in_days</code> (<code>30</code>,{" "}
            <code>90</code>, <code>365</code> or <code>null</code> for never). Answers{" "}
            <code>201</code> with <code>{"{key, secret}"}</code>: the key&apos;s record and
            the full plaintext key, the only time it is ever returned.
          </p>
        </Endpoint>
        <Endpoint method="DELETE" path="/api/keys/{key_id}" scope="session">
          <p>
            Revokes the key and returns its record. Revoking twice is harmless and returns the
            same record; a key id that is not yours is <code>404</code>.
          </p>
        </Endpoint>
      </ul>
      <p>A key record, as the list and the other two calls return it:</p>
      <CodeBlock code={KEY_OUT} lang="json" title="ApiKeyOut" />
    </DocsPage>
  );
}
