"use client";

import { useEffect, useRef, useState } from "react";
import { authClient } from "@/lib/auth/client";
import { GITHUB_URL } from "@/lib/site";
import { getMe, peekMe, type Me } from "./MeApi";
import Skeleton from "./Skeleton";

/**
 * The account section of Settings: who you are signed in as, whether this
 * instance treats you as its owner or as a demo, and the way out.
 *
 * Sign-out is the reason this exists. Until the app went multi-user there was
 * only ever one account and no door: no way to see whose diary you were
 * looking at, and no way to leave. `authClient.signOut()` clears the Neon Auth
 * session cookie; a full reload (rather than a router push) drops every
 * component's cached state with it, so nothing of the previous account can
 * survive on screen.
 */
export default function AccountCard() {
  // what this visit already read paints at once; the read below refreshes
  // the quota
  const [me, setMe] = useState<Me | null>(peekMe);
  const [failed, setFailed] = useState(false);
  const [leaving, setLeaving] = useState(false);
  // Sign out is two taps: getting back in means signing in again
  const [confirming, setConfirming] = useState(false);
  const signOutBtn = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let live = true;
    getMe()
      .then((m) => live && setMe(m))
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
    };
  }, []);

  const signOut = async () => {
    setLeaving(true);
    try {
      await authClient.signOut();
    } catch {
      // Even a failed call should not strand someone on a signed-in screen:
      // the reload re-runs the proxy, which sends a session-less visitor to
      // the landing page.
    }
    window.location.href = "/";
  };

  const quota = me?.demo ?? null;
  const spent = quota ? quota.used_today >= quota.limit : false;

  return (
    <section className="m3-section" aria-label="Account">
      <h2>Account</h2>
      <div className="m3-card account-card">
        <div className="account-who">
          <span className="account-label">Signed in as</span>
          <span className="account-email">
            {me?.email ?? (failed ? "—" : <Skeleton height={14} width={160} />)}
          </span>
        </div>

        {me ? (
          <div className="account-kind">
            {me.is_owner ? (
              <>
                <span className="account-badge owner">Owner</span>
                <span className="account-note">No daily limit.</span>
              </>
            ) : (
              <>
                <span className="account-badge demo">Demo</span>
                <span className="account-note">
                  {quota
                    ? spent
                      ? `Today's log is used. The next one is available at ${localReset(quota.resets_at)}.`
                      : `${quota.limit - quota.used_today} of ${quota.limit} log${quota.limit === 1 ? "" : "s"} left today.`
                    : "One log a day."}{" "}
                  To keep a real diary, run your own copy of Tally — it’s{" "}
                  <a href={GITHUB_URL}>open source</a>.
                </span>
              </>
            )}
          </div>
        ) : null}

        {failed ? (
          <p className="account-note">
            Couldn’t reach the account service. You can still sign out.
          </p>
        ) : null}

        {confirming ? (
          <div className="m3-actions confirm">
            <span className="m3-note">Sign out of this device?</span>
            <button
              type="button"
              className="m3-link-btn"
              disabled={leaving}
              onClick={() => {
                setConfirming(false);
                requestAnimationFrame(() => signOutBtn.current?.focus());
              }}
              autoFocus
            >
              Cancel
            </button>
            <button
              type="button"
              className="m3-link-btn danger"
              disabled={leaving}
              onClick={signOut}
            >
              {leaving ? "Signing out…" : "Sign out"}
            </button>
          </div>
        ) : (
          <button
            ref={signOutBtn}
            type="button"
            className="account-signout"
            onClick={() => setConfirming(true)}
          >
            Sign out
          </button>
        )}
      </div>
    </section>
  );
}

/** The 429 body and /api/me both carry the reset as a UTC instant; only the
 *  UI turns it into the reader's clock. */
function localReset(iso: string): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return "the next UTC midnight";
  const sameDay = t.toDateString() === new Date().toDateString();
  const time = t.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  return sameDay ? time : `${time} tomorrow`;
}
