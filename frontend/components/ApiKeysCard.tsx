"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import {
  createKey,
  listKeys,
  revokeKey,
  type ApiKey,
  type ApiKeyCreate,
} from "./ApiKeysApi";

type Access = ApiKeyCreate["scopes"];
type Expiry = NonNullable<ApiKeyCreate["expires_in_days"]> | null;
type Status = "active" | "expired" | "revoked";
type Copy = "idle" | "copied" | "selected";

const ACCESS: { value: Access; label: string; note: string }[] = [
  {
    value: "read",
    label: "Read only",
    note: "Reads your diary, history, goals, weights and foods. Cannot log or change anything.",
  },
  {
    value: "read_write",
    label: "Read & write",
    note: "Also logs food and weight, edits and deletes entries, and changes settings. Everything but managing keys.",
  },
];

const EXPIRY: { value: Expiry; label: string }[] = [
  { value: null, label: "Never" },
  { value: 30, label: "30 days" },
  { value: 90, label: "90 days" },
  { value: 365, label: "365 days" },
];

const STATUS_TEXT: Record<Status, string> = {
  active: "Active",
  expired: "Expired",
  revoked: "Revoked",
};

const RTF = new Intl.RelativeTimeFormat(undefined, { numeric: "always" });
const STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 365 * 86_400],
  ["month", 30 * 86_400],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];

/** "3 days ago". Last-used moves at most every five minutes server-side, so
 *  nothing finer than a minute is worth printing. */
function ago(iso: string, now: number): string {
  const secs = (now - Date.parse(iso)) / 1000;
  const step = STEPS.find(([, size]) => secs >= size);
  return step ? RTF.format(-Math.floor(secs / step[1]), step[0]) : "just now";
}

/** "Oct 23", with the year only when it is not this one. An expiry is a
 *  deadline, so it reads better as a date than as "in 12 months". */
function shortDate(iso: string, now: number): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: d.getFullYear() === new Date(now).getFullYear() ? undefined : "numeric",
  });
}

function When({ iso, text }: { iso: string; text: string }) {
  return (
    <time dateTime={iso} title={new Date(iso).toLocaleString()}>
      {text}
    </time>
  );
}

function statusOf(k: ApiKey, now: number): Status {
  if (k.revoked_at) return "revoked";
  if (k.expires_at && Date.parse(k.expires_at) <= now) return "expired";
  return "active";
}

/** The server's own sentence when it gave one (409 "you already have 10
 *  active API keys; revoke one first"), else the fallback. */
function detailOr(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.detail ? e.detail : fallback;
}

/**
 * Settings § API keys: personal keys for scripts and AI agents (REST or MCP),
 * self-contained like AccountCard, so it loads and saves apart from the
 * page's one Save.
 *
 * The secret exists in the browser only between the create response and
 * Done: the server keeps a hash, so the banner is the one chance to copy it.
 * Revoking keeps the row (dimmed) so the list still says what a key was and
 * when it last worked. Access and expiry are fixed at creation; changing
 * either means a new key.
 */
export default function ApiKeysCard() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const [sheet, setSheet] = useState(false);
  const [label, setLabel] = useState("");
  const [access, setAccess] = useState<Access>("read_write");
  const [expiry, setExpiry] = useState<Expiry>(null);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [reveal, setReveal] = useState<{ label: string; secret: string } | null>(null);
  const [copied, setCopied] = useState<Copy>("idle");
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);

  const newBtn = useRef<HTMLButtonElement>(null);
  const copyBtn = useRef<HTMLButtonElement>(null);
  const secretEl = useRef<HTMLElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const refocusRevoke = useRef<string | null>(null);

  useEffect(() => {
    let live = true;
    setLoadError(null);
    listKeys()
      .then((k) => live && setKeys(k))
      .catch((e) => live && setLoadError(detailOr(e, "Couldn't load your API keys.")));
    return () => {
      live = false;
    };
  }, [attempt]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  // The banner takes focus when it appears: its Copy button is the next
  // thing to do, and the sheet that had focus is gone.
  useEffect(() => {
    if (reveal) copyBtn.current?.focus();
  }, [reveal]);

  // Cancel swaps the confirm buttons back for a fresh Revoke button; put
  // focus on it rather than letting it fall to the page.
  useEffect(() => {
    const id = refocusRevoke.current;
    if (confirmId !== null || !id) return;
    refocusRevoke.current = null;
    document.querySelector<HTMLButtonElement>(`[data-revoke="${id}"]`)?.focus();
  }, [confirmId]);

  const closeSheet = useCallback(() => {
    setSheet(false);
    newBtn.current?.focus();
  }, []);

  useEffect(() => {
    if (!sheet) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeSheet();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sheet, closeSheet]);

  // A fresh sheet every time. It opens on Read & write, the API's own
  // default: logging food is what most agents are for.
  const openSheet = () => {
    setLabel("");
    setAccess("read_write");
    setExpiry(null);
    setCreateError(null);
    setSheet(true);
  };

  const create = async (e: FormEvent) => {
    e.preventDefault();
    const name = label.trim();
    if (!name || creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const made = await createKey({ label: name, scopes: access, expires_in_days: expiry });
      // Prepend only onto a loaded list. The refetch covers a list that failed
      // or is still loading (that GET predates the insert), and drops its answer.
      setKeys((ks) => ks && [made.key, ...ks]);
      setAttempt((n) => n + 1);
      setCopied("idle");
      setReveal({ label: made.key.label, secret: made.secret });
      setSheet(false);
    } catch (err) {
      setCreateError(detailOr(err, "Couldn't create the key — try again."));
    } finally {
      setCreating(false);
    }
  };

  const copy = async () => {
    if (!reveal) return;
    let next: Copy = "copied";
    try {
      await navigator.clipboard.writeText(reveal.secret);
    } catch {
      // No async clipboard (a plain-http origin, a refused permission):
      // select the whole key so the system's own copy takes it.
      const sel = window.getSelection();
      if (secretEl.current && sel) {
        const range = document.createRange();
        range.selectNodeContents(secretEl.current);
        sel.removeAllRanges();
        sel.addRange(range);
      }
      next = "selected";
    }
    setCopied(next);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied("idle"), 2000);
  };

  const done = () => {
    setReveal(null);
    newBtn.current?.focus();
  };

  const revoke = async (id: string) => {
    setRevoking(true);
    setError(null);
    try {
      const row = await revokeKey(id);
      setKeys((ks) => ks && ks.map((k) => (k.id === row.id ? row : k)));
      setConfirmId(null);
      // The row's buttons go with its active status; New key is always there.
      newBtn.current?.focus();
    } catch (err) {
      setError(detailOr(err, "Couldn't revoke the key — try again."));
    } finally {
      setRevoking(false);
    }
  };

  const now = Date.now();
  // Live keys first; the server's newest-first order holds within each group.
  const rows = (keys ?? [])
    .map((k) => ({ k, st: statusOf(k, now) }))
    .sort((a, b) => Number(a.st !== "active") - Number(b.st !== "active"));
  const active = rows.filter((r) => r.st === "active").length;

  return (
    <section className="m3-section m3-wide" aria-label="API keys">
      <h2>
        API keys{" "}
        {active ? <span className="count">{active} active</span> : null}
      </h2>
      <div className="m3-card">
        <p className="m3-note">
          For scripts and AI agents, over the REST API or MCP. A key acts as
          you, with your account&rsquo;s limits.
        </p>
        <div className="m3-actions">
          <button
            type="button"
            className="keys-btn accent"
            ref={newBtn}
            onClick={openSheet}
          >
            New key
          </button>
          <Link href="/docs/api-keys" className="m3-link-btn">
            How to use keys →
          </Link>
        </div>

        {reveal ? (
          <div className="keys-reveal">
            <p className="keys-reveal-title" id="keys-reveal-warn">
              Copy it now, it will not be shown again
            </p>
            <p className="m3-note">
              &ldquo;{reveal.label}&rdquo; is ready. Tally keeps only a hash of
              it; if you lose it, revoke it and make another.
            </p>
            <code className="keys-secret" ref={secretEl}>
              {reveal.secret}
            </code>
            <div className="keys-reveal-actions">
              <button
                type="button"
                className="keys-btn accent"
                ref={copyBtn}
                onClick={copy}
                aria-describedby="keys-reveal-warn"
              >
                {copied === "copied" ? "Copied" : copied === "selected" ? "Selected" : "Copy key"}
              </button>
              <button type="button" className="keys-btn" onClick={done}>
                Done
              </button>
            </div>
            <span className="keys-sr" aria-live="polite">
              {copied === "copied"
                ? "Copied"
                : copied === "selected"
                  ? "Key selected. Copy it with your keyboard or your device's menu."
                  : ""}
            </span>
          </div>
        ) : null}

        {error ? (
          <p className="sheet-error" role="alert">
            {error}
          </p>
        ) : null}

        {keys === null ? (
          loadError ? (
            <div className="notice error" role="alert">
              <span>{loadError}</span>
              <button
                type="button"
                className="notice-btn"
                onClick={() => setAttempt((n) => n + 1)}
              >
                Retry
              </button>
            </div>
          ) : (
            <p className="m3-note">Loading…</p>
          )
        ) : rows.length === 0 ? (
          <p className="m3-note">No keys yet.</p>
        ) : (
          <ul className="keys-list">
            {rows.map(({ k, st }) => (
              <li key={k.id} className={`keys-row ${st}`}>
                <div className="keys-row-head">
                  <span className="keys-label">{k.label}</span>
                  <span className={`keys-status ${st}`}>{STATUS_TEXT[st]}</span>
                </div>
                <code className="keys-redacted">{k.redacted}</code>
                <dl className="keys-meta">
                  <div>
                    <dt>Access</dt>
                    <dd>{k.scopes.includes("write") ? "Read & write" : "Read only"}</dd>
                  </div>
                  <div>
                    <dt>Created</dt>
                    <dd>
                      <When iso={k.created_at} text={ago(k.created_at, now)} />
                    </dd>
                  </div>
                  <div>
                    <dt>Last used</dt>
                    <dd>
                      {k.last_used_at ? (
                        <When iso={k.last_used_at} text={ago(k.last_used_at, now)} />
                      ) : (
                        "Never"
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>{st === "expired" ? "Expired" : "Expires"}</dt>
                    <dd>
                      {k.expires_at ? (
                        <When iso={k.expires_at} text={shortDate(k.expires_at, now)} />
                      ) : (
                        "Never"
                      )}
                    </dd>
                  </div>
                </dl>
                {st === "active" ? (
                  <div className="keys-row-actions">
                    {confirmId === k.id ? (
                      <>
                        <span className="m3-note">
                          Anything using it stops working on its next request.
                        </span>
                        <button
                          type="button"
                          className="m3-link-btn"
                          disabled={revoking}
                          onClick={() => {
                            refocusRevoke.current = k.id;
                            setConfirmId(null);
                          }}
                          autoFocus
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="m3-link-btn danger"
                          disabled={revoking}
                          onClick={() => revoke(k.id)}
                          aria-label={`Yes, revoke ${k.label}`}
                        >
                          {revoking ? "Revoking…" : "Yes, revoke"}
                        </button>
                      </>
                    ) : (
                      <button
                        type="button"
                        className="m3-link-btn danger"
                        onClick={() => {
                          setError(null);
                          setConfirmId(k.id);
                        }}
                        aria-label={`Revoke ${k.label}`}
                        data-revoke={k.id}
                      >
                        Revoke
                      </button>
                    )}
                  </div>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </div>

      {sheet ? (
        <>
          <div className="subsheet-backdrop" onClick={closeSheet} />
          <div className="subsheet" role="dialog" aria-labelledby="keys-sheet-title">
            <form className="keys-form" onSubmit={create}>
              <div>
                <div className="subsheet-title" id="keys-sheet-title">
                  New API key
                </div>
                <p className="subsheet-hint">
                  One key per script or agent, so revoking one breaks nothing
                  else. You see it once, right after you create it.
                </p>
              </div>

              <label className="m3-field">
                Label
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  maxLength={80}
                  placeholder="Claude Code on my laptop"
                  autoComplete="off"
                  spellCheck={false}
                  autoFocus
                />
              </label>

              <div role="group" aria-labelledby="keys-access-label">
                <div className="edit-label" id="keys-access-label">
                  Access
                </div>
                <div className="m3-seg">
                  {ACCESS.map((a) => (
                    <button
                      key={a.value}
                      type="button"
                      className={access === a.value ? "active" : undefined}
                      aria-pressed={access === a.value}
                      onClick={() => setAccess(a.value)}
                    >
                      {a.label}
                    </button>
                  ))}
                </div>
                <p className="m3-note keys-seg-note">
                  {ACCESS.find((a) => a.value === access)?.note}
                </p>
              </div>

              <div role="group" aria-labelledby="keys-expiry-label">
                <div className="edit-label" id="keys-expiry-label">
                  Expires
                </div>
                <div className="m3-seg">
                  {EXPIRY.map((x) => (
                    <button
                      key={x.label}
                      type="button"
                      className={expiry === x.value ? "active" : undefined}
                      aria-pressed={expiry === x.value}
                      onClick={() => setExpiry(x.value)}
                    >
                      {x.label}
                    </button>
                  ))}
                </div>
              </div>

              {createError ? (
                <p className="sheet-error" role="alert">
                  {createError}
                </p>
              ) : null}

              <button
                type="submit"
                className="commit-btn"
                disabled={creating || !label.trim()}
              >
                {creating ? "Creating…" : "Create key"}
              </button>
              <button type="button" className="m3-link-btn keys-cancel" onClick={closeSheet}>
                Cancel
              </button>
            </form>
          </div>
        </>
      ) : null}
    </section>
  );
}
