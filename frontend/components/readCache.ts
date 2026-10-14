/**
 * The last answer each tab root read, so a return to the tab paints it at
 * once instead of a loading state (stale-while-revalidate). Every page still
 * runs its mount read and puts the fresh answer back; this only fills the
 * gap until it lands. That full-height first paint is also what lets
 * useScrollMemory put the page back where it was.
 *
 * Client only: on the server a module Map would be shared by every request,
 * so put() does nothing there. It lasts as long as the document; signing out
 * reloads, which drops it. A hard load starts empty on server and client
 * alike, so hydration never sees a cached value. /api/me is not kept here:
 * MeApi already holds the newest read (peekMe).
 *
 *   const [x, setX] = useState<X | null>(() => peek<X>("key"));
 *   ...then, on the fresh answer: put("key", x);
 */
const cache = new Map<string, unknown>();

export function peek<T>(key: string): T | null {
  return (cache.get(key) as T | undefined) ?? null;
}

export function put(key: string, value: unknown): void {
  if (typeof window === "undefined") return;
  cache.set(key, value);
}
