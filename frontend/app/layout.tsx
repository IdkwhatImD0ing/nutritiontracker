import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Archivo, Bricolage_Grotesque } from "next/font/google";
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

const description =
  "Voice-first nutrition logger: say what you ate and Tally finds the foods, works out the macros, and keeps your diary. Photo, barcode, and search lanes too.";

// Public-facing (the repo is open source and anyone can sign up for a demo
// account). No social image exists yet, so openGraph/twitter carry text only.
// Absolute URLs for Open Graph and Twitter cards. Without it Next warns and
// emits relative URLs, which link previews cannot resolve. NEXT_PUBLIC_SITE_URL
// lets a fork point it at its own domain without editing this file.
const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "https://tally.art3m1s.me";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
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

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${archivo.variable} ${bricolage.variable}`}>
      <body>
        <ServiceWorkerProvider>
          <LegacyChatCleanup />
          {children}
        </ServiceWorkerProvider>
      </body>
    </html>
  );
}
