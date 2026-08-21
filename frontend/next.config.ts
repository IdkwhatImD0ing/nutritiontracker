import { withSerwist } from "@serwist/turbopack";
import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  agentRules: false,
};

// Adds esbuild to serverExternalPackages so the /serwist route handler can
// bundle app/sw.ts at build time; the SW itself is configured in that route.
export default withSerwist(nextConfig);
