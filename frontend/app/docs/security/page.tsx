import type { Metadata } from "next";
import Link from "next/link";
import { DocsPage } from "@/components/docs/DocsPage";
import { GITHUB_URL } from "@/lib/site";

/** How keys are protected, as built: api_keys.py (hashing), migration 006
 *  (RLS on api_keys, the api_key_auth function), auth.py (scopes, the
 *  session-only /api/keys, the origin check). Describe the system, not
 *  aspirations. */

export const metadata: Metadata = {
  title: "Security",
  description:
    "How Tally stores API keys, enforces scopes, keeps accounts apart, and handles revocation.",
};

const TOC = [
  { id: "storage", label: "How keys are stored" },
  { id: "scopes", label: "Scopes" },
  { id: "isolation", label: "Accounts stay apart" },
  { id: "revocation", label: "Revocation and expiry" },
  { id: "keys-cannot-manage-keys", label: "Keys cannot manage keys" },
  { id: "browsers", label: "Browsers" },
  { id: "practice", label: "Good practice" },
];

export default function SecurityDoc() {
  return (
    <DocsPage href="/docs/security" toc={TOC}>
      <h1>Security</h1>
      <p className="docs-lede">
        What protects your diary when a key is in play, described as it is built. The code is{" "}
        <a href={GITHUB_URL}>open source</a> if you want to check.
      </p>

      <h2 id="storage">How keys are stored</h2>
      <p>
        Tally stores only the SHA-256 hash of each full key, plus its id and last four characters
        for the list. The key itself is shown once, when you create it, and never again. The
        secret part is 256 random bits, so a hash that leaked could not be turned back into a
        working key by guessing. If you lose a key, revoke it and make a new one.
      </p>

      <h2 id="scopes">Scopes</h2>
      <p>
        A key is either read only or read &amp; write, and the check runs on every request. On the
        REST API, a read-only key may only use routes that read. By default those are GET, HEAD
        and OPTIONS; two GETs that can change stored data need write, and one POST that saves
        nothing needs only read. Anything else is refused with <code>403</code>. On the MCP
        server, a read-only key is served a separate tool set that has no write tools at all, so
        there is nothing to call by mistake.{" "}
        <Link href="/docs/api-keys#scopes">Scopes in detail</Link>.
      </p>

      <h2 id="isolation">Accounts stay apart</h2>
      <p>
        Every request runs as one account inside Postgres row-level security. The app connects as
        a database role that cannot bypass it, so a query only ever sees that account&apos;s rows,
        whatever the code asks for. Your diary, logs, saved meals, settings, weigh-ins and API
        keys are all under that rule; the food records they point to are a shared cache. Another
        account&apos;s ids simply read as <code>404</code>.
      </p>
      <p>
        One step has to happen before the account is known: finding whose key was presented.
        That is a single narrow database function. Given a key id and a hash, it answers only
        &ldquo;whose live key is this?&rdquo;, and only when both match and the key is neither
        revoked nor expired. The right to run it is withdrawn from the public and granted
        only to the app&apos;s database role. It also reads the account&apos;s current email, so an
        email change applies from the next request.
      </p>

      <h2 id="revocation">Revocation and expiry</h2>
      <p>
        Revoked and expired keys are checked on every request, with no cache in between, so a
        revoked key fails on its very next call. Revoked, expired, unknown and malformed keys all
        get the same <code>401 invalid API key</code>, so the answer never reveals which keys
        exist. A revoked key stays in your list, dimmed, as a record. An account keeps at most 50
        keys, so a new key past that deletes the oldest revoked or expired ones.
      </p>

      <h2 id="keys-cannot-manage-keys">Keys cannot manage keys</h2>
      <p>
        Creating, listing and revoking keys needs a signed-in app session. A key presented to{" "}
        <code>/api/keys</code> gets <code>403</code>. So a leaked key cannot mint a replacement
        for itself, cannot see your other keys, and cannot undo its own revocation.
      </p>

      <h2 id="browsers">Browsers</h2>
      <p>
        The API sends no CORS headers, so a web page on another site cannot read its responses.
        A signed-in app session is also held to the app&apos;s own origin: a state-changing
        request with a session token and another site&apos;s <code>Origin</code> header is
        refused with <code>403 origin mismatch</code>. Requests with an API key are not checked
        for <code>Origin</code>. The key is the whole credential and a browser never adds it to a
        request by itself, so another site has nothing to forge, and a client that runs in a
        browser engine, such as an extension, can use a key. Keys belong in servers, scripts and
        agent configs, never in a web page, where anyone could read them.
      </p>

      <h2 id="practice">Good practice</h2>
      <ul>
        <li>Give an agent that only answers questions a read-only key.</li>
        <li>Set an expiry on keys for experiments; 30 days is plenty for trying a client.</li>
        <li>
          Use one key per client, labelled with where it lives, so revoking one does not break the
          rest.
        </li>
        <li>
          Keep keys in environment variables or a secret store. Never commit them, and never paste
          them into a chat or an issue.
        </li>
        <li>
          If a key may have leaked, revoke it in <Link href="/settings">Settings</Link> first, then
          make a new one.
        </li>
      </ul>
    </DocsPage>
  );
}
