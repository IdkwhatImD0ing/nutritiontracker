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
 * a 307 to the landing page is not an image.
 *
 * `/` serves two different pages. Signed in, it is Today. Signed out, it is
 * the landing page — rendered AT `/`, not redirected to, so the front door
 * has the address people actually share. The auth middleware is the one that
 * knows which: when it wants to bounce an anonymous visitor off `/`, we turn
 * that redirect into a rewrite of the landing route and keep the URL. Every
 * other protected path still redirects, and lands on the same page.
 */
const LANDING = "/welcome";

export default async function proxy(request: NextRequest) {
  const response = await getAuth().middleware({ loginUrl: LANDING })(request);

  if (request.nextUrl.pathname !== "/") return response;

  const location = response?.headers?.get("location");
  if (!location) return response; // a session: fall through to Today

  const target = new URL(location, request.url);
  if (target.pathname !== LANDING) return response; // some other redirect

  return NextResponse.rewrite(new URL(LANDING, request.url));
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|auth|welcome|serwist|manifest.webmanifest|icons|apple-touch-icon.png|og.png|screenshots).*)",
  ],
};
