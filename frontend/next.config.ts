import { withSerwist } from "@serwist/turbopack";
import type { NextConfig } from "next";

// Local dev only: Vercel's service rewrite (vercel.json) sends /api/* to
// FastAPI in deployments; `next dev` has no such router, so forward to a
// local uvicorn. Rewrites run after file-system routes, so the Neon Auth
// handler at app/api/auth/[...path] keeps winning for /api/auth/*.
const devApi = process.env.DEV_API_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  agentRules: false,
  async rewrites() {
    if (process.env.NODE_ENV !== "development") return [];
    return [{ source: "/api/:path*", destination: `${devApi}/api/:path*` }];
  },
};

// Adds esbuild to serverExternalPackages so the /serwist route handler can
// bundle app/sw.ts at build time; the SW itself is configured in that route.
export default withSerwist(nextConfig);
