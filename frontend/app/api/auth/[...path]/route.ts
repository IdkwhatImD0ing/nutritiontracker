import { getAuth } from "@/lib/auth/server";

type Ctx = { params: Promise<{ path: string[] }> };

// Wrapped (rather than `export const { GET, POST } = auth.handler()`) so the
// handler is built per-request: next build evaluates this module without env.
export function GET(request: Request, ctx: Ctx) {
  return getAuth().handler().GET(request, ctx);
}

export function POST(request: Request, ctx: Ctx) {
  // Registration never happens over the public REST surface: the one door is
  // the sign-up server action (frontend/app/auth/actions.ts), which calls the
  // auth service directly — so the demo copy, and any rate limit a burst of
  // sign-ups ever needs (BACKLOG 103), live in one place.
  if (new URL(request.url).pathname.includes("sign-up")) {
    return new Response("sign-up is disabled", { status: 403 });
  }
  return getAuth().handler().POST(request, ctx);
}
