import type { NextRequest } from "next/server";
import { getAuth } from "@/lib/auth/server";

/**
 * Next 16 proxy (the middleware successor): protect everything except the
 * auth pages. /api is excluded — /api/auth/* must reach the handler route,
 * and every other /api/* route is FastAPI's, which verifies the Bearer JWT
 * itself (§2).
 */
export default function proxy(request: NextRequest) {
  return getAuth().middleware({ loginUrl: "/auth/sign-in" })(request);
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|auth).*)"],
};
