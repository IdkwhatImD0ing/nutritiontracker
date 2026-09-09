"use client";

import { useEffect } from "react";

/**
 * One-time cleanup, 2026-09-08. The Agent chat used to be kept on the device
 * (`tally.chat.v1`, and `tally.chat.v1:<email>` once the key went per-account)
 * until every opening of the lane became a new conversation (§6.4). Nothing
 * reads those keys any more, and "the previous conversation is cleared" covers
 * the stored one — so this runs in the app shell rather than on Today, which a
 * user who lives on History or Settings may never mount.
 *
 * Renders nothing. Delete once the devices in use have all run this build.
 */
export function LegacyChatCleanup() {
  useEffect(() => {
    try {
      for (const k of Object.keys(localStorage)) {
        if (k.startsWith("tally.chat.")) localStorage.removeItem(k);
      }
    } catch {
      /* storage unavailable — there is nothing to clean */
    }
  }, []);
  return null;
}
