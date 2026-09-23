/** Where this deployment lives, defined once. The root layout's metadataBase,
 *  the landing page and the docs snippets all print it, so they cannot drift.
 *  NEXT_PUBLIC_SITE_URL lets a fork point it at its own domain without editing
 *  code; being NEXT_PUBLIC_, it is inlined at build time on server and client. */
export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "https://tally.art3m1s.me";

export const GITHUB_URL = "https://github.com/IdkwhatImD0ing/nutritiontracker";
