import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Archivo, Bricolage_Grotesque, IBM_Plex_Mono } from "next/font/google";
import { NavTrail } from "@/components/BackBar";
import { KeyboardInset } from "@/components/KeyboardInset";
import { SITE_URL } from "@/lib/site";
import SPLASH_SCREENS from "@/lib/splash-screens.json";
import "./globals.css";
import { LegacyChatCleanup } from "./legacy-storage";
import { ServiceWorkerProvider } from "./serwist/provider";

// Self-hosted at build time: no runtime request to Google, no layout shift.
// Archivo was named in globals.css from the start but never actually loaded —
// the app rendered in system-ui until this landed.
const archivo = Archivo({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
  variable: "--font-archivo",
  display: "swap",
});

const bricolage = Bricolage_Grotesque({
  subsets: ["latin"],
  weight: ["500", "700", "800"],
  variable: "--font-bricolage",
  display: "swap",
});

// Code, API key strings and the small uppercase labels on the public pages
// (landing, docs). Exposed as --font-mono in globals.css.
const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500"],
  variable: "--font-plex-mono",
  display: "swap",
});

const description =
  "Voice-first nutrition logger: say what you ate and Tally finds the foods, works out the macros, and keeps your diary. Photo, barcode, and search lanes too.";

// Public-facing (the repo is open source and anyone can sign up for a demo
// account). No social image exists yet, so openGraph/twitter carry text only.
// Absolute URLs for Open Graph and Twitter cards. Without it Next warns and
// emits relative URLs, which link previews cannot resolve. SITE_URL reads
// NEXT_PUBLIC_SITE_URL, so a fork can point it at its own domain (lib/site.ts).

// iOS launch images, one per screen (CSS size × pixel ratio), portrait only. Safari
// ignores the manifest's background_color, so without an image whose media query
// matches the screen exactly, a cold start of the installed app shows white.
// One table (lib/splash-screens.json) feeds both these media queries and
// scripts/gen-icons.py, which draws the PNGs.

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  applicationName: "Tally",
  title: { default: "Tally", template: "%s · Tally" },
  description,
  keywords: [
    "nutrition tracker",
    "food diary",
    "calorie counter",
    "macro tracker",
    "voice logging",
    "open source",
    "PWA",
  ],
  openGraph: {
    title: "Tally",
    description,
    siteName: "Tally",
    type: "website",
    url: "/",
    locale: "en_US",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        type: "image/png",
        alt: "Tally — say what you ate.",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Tally",
    description,
    images: ["/og.png"],
  },
  // iOS installed-PWA chrome: standalone capable, dark ground under a
  // translucent status bar (globals.css already pads safe-area insets).
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Tally",
    startupImage: SPLASH_SCREENS.map(({ w, h, dpr }) => ({
      url: `/splash/${w * dpr}x${h * dpr}.png`,
      media: `(device-width: ${w}px) and (device-height: ${h}px) and (-webkit-device-pixel-ratio: ${dpr}) and (orientation: portrait)`,
    })),
  },
  formatDetection: {
    telephone: false,
  },
  // Next emits only the standard `mobile-web-app-capable`; older iOS reads the
  // apple-prefixed name, so emit both.
  other: {
    "apple-mobile-web-app-capable": "yes",
  },
  // Declared explicitly rather than via app/ file conventions, so the emitted
  // <link> set is greppable in one place. Order is the browser's preference
  // order: .ico first for the ones that only ever ask for /favicon.ico, then
  // the SVG that every current browser prefers and scales cleanly.
  icons: {
    icon: [
      { url: "/favicon.ico", sizes: "16x16 32x32 48x48" },
      { url: "/icons/icon.svg", type: "image/svg+xml" },
      { url: "/icons/icon-192.png", type: "image/png", sizes: "192x192" },
      { url: "/icons/icon-512.png", type: "image/png", sizes: "512x512" },
    ],
    apple: [{ url: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#111113",
  // globals.css defines one (dark) palette with no light counterpart, so say so
  // rather than leaving the UA to guess: this is what makes form controls,
  // scrollbars and the tap-highlight render dark instead of stock light.
  colorScheme: "dark",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

// The iPhone's Text Size / Larger Text setting (owner decision, 2026-10-13):
// an installed app has no Safari aA menu, so a probe in `font:
// -apple-system-body` reads the size iOS gives body text (17px by default)
// and the root follows it, probe / 17 × 16px clamped to 16–20.8px (1.3×, what
// the layouts absorb). Re-read on return from Settings. A browser without the
// keyword rejects it (an empty style) and keeps its own root. Inline in <head>
// so the first paint is at size; tab-bar labels stay in px (m3.css).
const TEXT_SIZE = `(function(){var d=document.documentElement;function fit(){var p=document.createElement("span");p.style.font="-apple-system-body";if(!p.style.length)return;d.appendChild(p);var px=parseFloat(getComputedStyle(p).fontSize);d.removeChild(p);if(px>0)d.style.fontSize=Math.min(Math.max(px/17*16,16),20.8)+"px"}fit();document.addEventListener("visibilitychange",function(){if(!document.hidden)fit()})})()`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html
      lang="en"
      className={`${archivo.variable} ${bricolage.variable} ${plexMono.variable}`}
      // TEXT_SIZE sets the root's font-size before React hydrates
      suppressHydrationWarning
    >
      <head>
        <script dangerouslySetInnerHTML={{ __html: TEXT_SIZE }} />
      </head>
      <body>
        <ServiceWorkerProvider>
          <LegacyChatCleanup />
          <NavTrail />
          <KeyboardInset />
          {children}
        </ServiceWorkerProvider>
      </body>
    </html>
  );
}
