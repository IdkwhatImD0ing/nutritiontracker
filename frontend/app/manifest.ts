import type { MetadataRoute } from "next";

/**
 * Web app manifest (served at /manifest.webmanifest; Next links it in <head>
 * automatically). Colors are the app's dark ground (globals.css --bg) per
 * §7.4's dark-mode-first visual language.
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Tally",
    short_name: "Tally",
    description: "Voice-first nutrition logging",
    id: "/",
    start_url: "/",
    display: "standalone",
    background_color: "#121715",
    theme_color: "#121715",
    icons: [
      // Full-bleed spruce-on-dark U mark; the glyph sits inside the maskable
      // safe zone, so the same assets serve both purposes.
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
      { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "maskable" },
      { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}
