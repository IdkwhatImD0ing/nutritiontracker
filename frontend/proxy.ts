import type { NextRequest } from "next/server";
import { getAuth } from "@/lib/auth/server";

/**
 * Next 16 proxy (the middleware successor): protect everything except the
 * auth pages. /api is excluded — /api/auth/* must reach the handler route,
 * and every other /api/* route is FastAPI's, which verifies the Bearer JWT
 * itself (§2). The PWA surface (manifest, icons, the Serwist service-worker
 * script) is excluded too: a sign-in redirect on those breaks install checks
 * and SW registration, and none of them carries user data.
 *
 * `/welcome` is the public landing page and is excluded as well. It is also
 * the redirect target: a signed-out visitor to `/` gets told what Tally is
 * before being asked for an email, and the page links on to sign-in/sign-up.
 */
export default function proxy(request: NextRequest) {
  return getAuth().middleware({ loginUrl: "/welcome" })(request);
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|auth|welcome|serwist|manifest.webmanifest|icons|apple-touch-icon.png).*)",
  ],
};
