import type { NextRequest } from "next/server";
import { getAuth } from "@/lib/auth/server";

/**
 * Next 16 proxy (the middleware successor): protect everything except the
 * auth pages. /api is excluded — /api/auth/* must reach the handler route,
 * and every other /api/* route is FastAPI's, which verifies the Bearer JWT
 * itself (§2). The PWA surface (manifest, icons, the Serwist service-worker
 * script) is excluded too: a sign-in redirect on those breaks install checks
 * and SW registration, and none of them carries user data.
 */
export default function proxy(request: NextRequest) {
  return getAuth().middleware({ loginUrl: "/auth/sign-in" })(request);
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|auth|serwist|manifest.webmanifest|icons|apple-touch-icon.png).*)",
  ],
};
