import type { MetadataRoute } from "next";

/**
 * Web app manifest (served at /manifest.webmanifest; Next links it in <head>
 * automatically). Colors are the app's dark ground (globals.css --bg) per
 * §7.4's dark-mode-first visual language.
 *
 * Chrome's install criteria want name, icons, start_url and display; the rest
 * here is what makes the installed app behave like an app rather than merely
 * qualify as one. Note `id` is fixed and must never change — it is the key
 * Chrome uses to decide whether a manifest describes an app already installed,
 * so editing it orphans everyone's install.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    id: "/",
    name: "Tally",
    short_name: "Tally",
    description:
      "Voice-first nutrition logging — say what you ate; Tally finds the foods and works out the macros.",
    lang: "en",
    dir: "ltr",
    start_url: "/",
    // Without an explicit scope Chrome infers it from start_url, which is the
    // same "/" here — but stating it is what keeps every in-app navigation
    // inside the installed window instead of bouncing to the browser.
    scope: "/",
    display: "standalone",
    // Portrait only: §7's bottom tab bar and the thumb-zone + are laid out for
    // a phone held upright, and nothing in the app rewards a landscape column.
    // iOS home-screen apps ignore this and rotate anyway, so globals.css and
    // m3.css carry a short-landscape guard (BACKLOG 158).
    orientation: "portrait",
    background_color: "#111113",
    theme_color: "#111113",
    categories: ["food", "health", "lifestyle"],
    icons: [
      // Two purposes, two files. The `any` art fills the square the way a
      // favicon should; the maskable art keeps every pixel inside the 80%
      // safe circle Android crops to, so the slash does not lose its ends.
      { src: "/icons/icon.svg", sizes: "any", type: "image/svg+xml", purpose: "any" },
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      {
        src: "/icons/icon-maskable-192.png",
        sizes: "192x192",
        type: "image/png",
        purpose: "maskable",
      },
      {
        src: "/icons/icon-maskable-512.png",
        sizes: "512x512",
        type: "image/png",
        purpose: "maskable",
      },
    ],
    // What Chrome shows in the richer install dialog instead of a bare name and
    // icon. Regenerate with scripts/gen-screenshots.mjs after a landing-page
    // change; `sizes` has to keep matching the file or Chrome drops the entry.
    // These are the signed-out front door, which is genuinely what someone
    // installing cold sees first -- BACKLOG 106 swaps in a real Today + staging
    // shot once there is a phone in hand.
    screenshots: [
      {
        src: "/screenshots/wide.png",
        sizes: "1280x800",
        type: "image/png",
        form_factor: "wide",
        label: "Tally on the desktop: say a meal, and the rows come back filled in.",
      },
      {
        src: "/screenshots/narrow.png",
        sizes: "824x1830",
        type: "image/png",
        form_factor: "narrow",
        label: "Tally on a phone: say a meal, and the rows come back filled in.",
      },
    ],
    // Long-press the installed icon. Only routes that exist and mean something
    // cold — logging itself is a sheet on Today, not an address, so there is no
    // honest "log a meal" deep link to offer here.
    shortcuts: [
      {
        name: "History",
        short_name: "History",
        description: "Past days, streaks and charts",
        url: "/history",
        icons: [{ src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" }],
      },
      {
        name: "Library",
        short_name: "Library",
        description: "Your foods and saved meals",
        url: "/library",
        icons: [{ src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" }],
      },
    ],
  };
}
