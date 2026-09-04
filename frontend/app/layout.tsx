import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { Archivo, Bricolage_Grotesque } from "next/font/google";
import "./globals.css";
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
  weight: ["500", "800"],
  variable: "--font-bricolage",
  display: "swap",
});

const description =
  "Voice-first nutrition logger: say what you ate and Tally finds the foods, works out the macros, and keeps your diary. Photo, barcode, and search lanes too.";

// Public-facing (the repo is open source and anyone can sign up for a demo
// account). No social image exists yet, so openGraph/twitter carry text only.
export const metadata: Metadata = {
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
  },
  twitter: {
    card: "summary",
    title: "Tally",
    description,
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
  icons: {
    icon: "/icons/icon-192.png",
    apple: "/apple-touch-icon.png",
  },
};

export const viewport: Viewport = {
  themeColor: "#121318",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${archivo.variable} ${bricolage.variable}`}>
      <body>
        <ServiceWorkerProvider>{children}</ServiceWorkerProvider>
      </body>
    </html>
  );
}
