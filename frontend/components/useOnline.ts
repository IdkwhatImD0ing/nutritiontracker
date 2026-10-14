"use client";

import { useSyncExternalStore } from "react";

/** §8.6: the app needs the network, and says so rather than failing a lane
 *  after the attempt. The same line on Today, History, Library and the +
 *  sheet. */
export const OFFLINE_NOTE = "You're offline. Logging needs a connection.";

function subscribe(onChange: () => void) {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

/** false while the browser reports no network. The server render and
 *  hydration assume online, so nothing flashes. iOS can report a dead or
 *  captive connection as online: there each screen's failure notice, with
 *  its Retry, is what speaks. */
export function useOnline(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => navigator.onLine,
    () => true,
  );
}
