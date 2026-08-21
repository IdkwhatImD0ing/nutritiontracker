import { getAuth } from "@/lib/auth/server";

type Ctx = { params: Promise<{ path: string[] }> };

// Wrapped (rather than `export const { GET, POST } = auth.handler()`) so the
// handler is built per-request: next build evaluates this module without env.
export function GET(request: Request, ctx: Ctx) {
  return getAuth().handler().GET(request, ctx);
}

export function POST(request: Request, ctx: Ctx) {
  // Single-user app: registration never happens over the public REST surface.
  // The one legitimate sign-up (first run) goes through the server action,
  // which calls the auth service directly and enforces the email allowlist.
  if (new URL(request.url).pathname.includes("sign-up")) {
    return new Response("sign-up is disabled", { status: 403 });
  }
  return getAuth().handler().POST(request, ctx);
}
