import { NextResponse, type NextRequest } from "next/server";
import { getAuth } from "@/lib/auth/server";

/**
 * Next 16 proxy (the middleware successor): protect everything except the
 * auth pages. /api is excluded — /api/auth/* must reach the handler route,
 * and every other /api/* route is FastAPI's, which verifies the Bearer JWT
 * itself (§2). The PWA surface (manifest, icons, the Serwist service-worker
 * script) is excluded too: a sign-in redirect on those breaks install checks
 * and SW registration, and none of them carries user data. `og.png` is on the
 * list for the same reason — a link unfurler is by definition signed out, and
 * a 307 to the landing page is not an image. `/privacy` and `/terms` are
 * excluded for the same reason again: a reviewer or an OAuth provider checking
 * them is signed out, and a bounce to the landing page reads as a missing page.
 *
 * `/` serves two different pages. Signed in, it is Today. Signed out, it is
 * the landing page — rendered AT `/`, not redirected to, so the front door
 * has the address people actually share. The auth middleware is the one that
 * knows which: when it wants to bounce an anonymous visitor off `/`, we turn
 * that redirect into a rewrite of the landing route and keep the URL.
 *
 * Every other protected path redirects to `/`, NOT to `/welcome`. Sending them
 * to `/welcome` gave one page two public addresses; `/` is the one people
 * share and it already carries the Sign in and Try a demo links, so `/welcome`
 * stays an internal rewrite target that nothing links to.
 *
 * Never hand the auth middleware `/` as its loginUrl, however tidy it looks.
 * Neon Auth lets through any path that is the login path or sits under it, and
 * every path sits under `/` — protection would switch off for the whole app.
 * It gets `/welcome`; we retarget the Location it produced, in place, so any
 * cookie it set on that response survives.
 */
const FRONT_DOOR = "/"; // where a signed-out visitor lands. NOT a loginUrl — see above
const LANDING = "/welcome"; // the middleware's loginUrl, and what `/` rewrites to

export default async function proxy(request: NextRequest) {
  const response = await getAuth().middleware({ loginUrl: LANDING })(request);

  const location = response?.headers?.get("location");
  // No Location means a session: fall through to the protected page.
  if (!location) return response;

  const target = new URL(location, request.url);
  if (target.pathname !== LANDING) return response; // some other redirect

  // `/` keeps its own address and renders the landing in place. The rewrite is
  // a new response, so carry over any cookie the middleware set on its own
  // (e.g. the clear of a stale session-data cookie).
  if (request.nextUrl.pathname === "/") {
    const rewrite = NextResponse.rewrite(new URL(LANDING, request.url));
    for (const cookie of response.headers.getSetCookie()) {
      rewrite.headers.append("set-cookie", cookie);
    }
    return rewrite;
  }

  // Everywhere else, send them to the front door instead of `/welcome`, with
  // the query string the middleware carried onto its login URL.
  const door = new URL(FRONT_DOOR, request.url);
  door.search = target.search;
  response.headers.set("location", door.toString());
  return response;
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|auth|welcome|(?:privacy|terms)(?:/|$)|serwist|manifest.webmanifest|icons|apple-touch-icon.png|og.png|screenshots).*)",
  ],
};
