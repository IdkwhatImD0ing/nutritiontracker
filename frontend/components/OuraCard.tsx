"use client";

import { useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import {
  connectOura,
  getOuraStatus,
  syncOura,
  type OuraStatus,
} from "./OuraApi";
import Skeleton from "./Skeleton";

/**
 * Settings' Oura section. Owner only: it renders nothing for a demo account,
 * or on a deployment without OURA_CLIENT_ID / OURA_CLIENT_SECRET (the server
 * answers `available: false`). Not connected: one Connect button. Connected:
 * when it last synced, Sync now, the last week's burn and steps, and
 * Reconnect — the way to grant a data type that was unticked on Oura's
 * consent screen (a reconnect re-reads the stored period, so it arrives with
 * its history). Oura's redirect back lands here with ?oura=, which is read
 * once and then dropped from the URL so a reload is quiet.
 */

/** What the card was on the last visit ("connected" or "available"), so the
 *  owner's Settings holds its place while the status loads instead of the
 *  tall connected card popping in above Daily targets. A demo account's
 *  status says available: false, which clears it: those keep rendering
 *  nothing. */
const SEEN_KEY = "tally.oura.card.v1";

/** A note's kind picks its look: only `ok` is the green status line; a
 *  cancel, a failure or a partial grant is a notice, never success green. */
type Note = { text: string; kind: "ok" | "warn" | "error" };

const RETURN_NOTES = new Map<string, Note>([
  ["connected", { text: "Oura connected.", kind: "ok" }],
  [
    "missing_scope",
    {
      text: "Oura connected, but without every permission Tally asks for. Reconnect and allow them all.",
      kind: "warn",
    },
  ],
  ["denied", { text: "Oura connection cancelled.", kind: "warn" }],
  ["error", { text: "Oura connection failed. Try again.", kind: "error" }],
  ["not_set_up", { text: "Oura isn't available on this server yet.", kind: "error" }],
]);

const SCOPE_LABELS: Record<string, string> = {
  daily: "daily activity",
  workout: "workouts",
};

function missingNote(missing: string[]): string | null {
  if (!missing.length) return null;
  const what = missing.map((s) => SCOPE_LABELS[s] ?? s).join(" or ");
  return `Oura isn't letting Tally read your ${what}. Reconnect and allow it.`;
}

function detail(e: unknown, fallback: string): string {
  return e instanceof ApiError && e.detail ? e.detail : fallback;
}

function dayLabel(iso: string): string {
  // noon, so no timezone can tip the date into the day before
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}

function ago(iso: string): string {
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  if (min < 48 * 60) return `${Math.round(min / 60)} h ago`;
  return new Date(iso).toLocaleDateString();
}

function amount(n: number | null, unit: string): string {
  return n == null ? `— ${unit}` : `${n.toLocaleString()} ${unit}`;
}

export default function OuraCard() {
  const [status, setStatus] = useState<OuraStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  // which request is in flight, so Sync now says "Syncing…" only for its own
  const [busy, setBusy] = useState<"sync" | "connect" | null>(null);
  // read from storage after mount (the server render has no storage)
  const [placeholder, setPlaceholder] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const back = params.get("oura");
    if (back) {
      setNote(RETURN_NOTES.get(back) ?? RETURN_NOTES.get("error")!);
      params.delete("oura");
      const qs = params.toString();
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${qs ? `?${qs}` : ""}`,
      );
    }
    let live = true;
    try {
      setPlaceholder(localStorage.getItem(SEEN_KEY));
    } catch {
      /* storage unavailable — the card just arrives */
    }
    getOuraStatus()
      .then((s) => {
        if (!live) return;
        setStatus(s);
        setPlaceholder(null);
        try {
          if (s.available) {
            localStorage.setItem(SEEN_KEY, s.connected ? "connected" : "available");
          } else {
            localStorage.removeItem(SEEN_KEY);
          }
        } catch {
          /* fine — no placeholder next visit */
        }
      })
      .catch((e) => {
        if (live) setPlaceholder(null);
        // Only the owner can get a 503 here (not migrated / not configured) —
        // anything else stays silent, so a demo account never sees the card.
        if (live && e instanceof ApiError && e.status === 503) {
          setError(detail(e, "Oura isn't available right now."));
        }
      });
    // Back from Oura's consent screen via the back/forward cache: the page is
    // restored as it was left, mid-"Opening Oura…".
    const onShow = (e: PageTransitionEvent) => {
      if (e.persisted) setBusy(null);
    };
    window.addEventListener("pageshow", onShow);
    return () => {
      live = false;
      window.removeEventListener("pageshow", onShow);
    };
  }, []);

  const connect = async () => {
    setBusy("connect");
    setError(null);
    try {
      await connectOura(); // navigates away on success
    } catch (e) {
      setError(detail(e, "Couldn't start the Oura connection."));
      setBusy(null);
    }
  };

  const syncNow = async () => {
    setBusy("sync");
    setError(null);
    setNote(null);
    try {
      const r = await syncOura(true);
      const missing = missingNote(r.missing);
      setNote(missing ? { text: missing, kind: "warn" } : null);
    } catch (e) {
      setError(detail(e, "Sync failed. Try again."));
    }
    // Re-read either way: a sync that found the grant revoked has deleted it,
    // and the card must turn back into Connect.
    try {
      setStatus(await getOuraStatus());
    } catch {
      /* keep what is shown */
    }
    setBusy(null);
  };

  if (!status?.available && !error) {
    return placeholder ? (
      <section className="m3-section" aria-label="Oura">
        <h2>Oura</h2>
        <Skeleton height={placeholder === "connected" ? 440 : 72} />
      </section>
    ) : null;
  }

  const hint = status?.connected && !note ? missingNote(status.missing) : null;

  return (
    <section className="m3-section" aria-label="Oura">
      <h2>Oura</h2>
      <div className="m3-card">
        {/* a div, as ErrorNotice's: a <p> would bring its UA margins */}
        {note?.kind === "ok" ? <p className="m3-status">{note.text}</p> : null}
        {note && note.kind !== "ok" ? (
          <div className={`notice ${note.kind}`}>
            <span>{note.text}</span>
          </div>
        ) : null}
        {hint ? (
          <div className="notice warn">
            <span>{hint}</span>
          </div>
        ) : null}
        {error ? <p className="sheet-error">{error}</p> : null}
        {status?.connected ? (
          <>
            <div className="m3-row">
              <span className="k">
                {status.last_synced_at
                  ? `Synced ${ago(status.last_synced_at)}`
                  : "Connected, not synced yet"}
              </span>
              <button
                type="button"
                className="m3-link-btn"
                disabled={busy !== null}
                aria-busy={busy === "sync"}
                onClick={syncNow}
              >
                {busy === "sync" ? (
                  <>
                    <span className="spinner" />
                    Syncing…
                  </>
                ) : (
                  "Sync now"
                )}
              </button>
            </div>
            {status.days.length ? (
              status.days.map((d) => (
                <div className="m3-row" key={d.day}>
                  <span className="k">{dayLabel(d.day)}</span>
                  <span>
                    {amount(d.total_calories, "kcal burned")} ·{" "}
                    {amount(d.steps, "steps")}
                  </span>
                </div>
              ))
            ) : (
              <p className="m3-note">No days from Oura this week yet.</p>
            )}
            <div className="m3-row">
              <span className="k">Missing workouts or daily activity?</span>
              <button
                type="button"
                className="m3-link-btn"
                disabled={busy !== null}
                onClick={connect}
              >
                Reconnect
              </button>
            </div>
          </>
        ) : status ? (
          <div className="m3-row">
            <span className="k">
              Bring in your daily burn, steps and workouts.
            </span>
            <button
              type="button"
              className="m3-link-btn"
              disabled={busy !== null}
              onClick={connect}
            >
              {busy === "connect" ? "Opening Oura…" : "Connect Oura"}
            </button>
          </div>
        ) : null}
      </div>
    </section>
  );
}
