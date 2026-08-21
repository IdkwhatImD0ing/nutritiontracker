import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { ServiceWorkerProvider } from "./serwist/provider";

export const metadata: Metadata = {
  applicationName: "Usual",
  title: "Usual",
  description: "Voice-first nutrition logging",
  // iOS installed-PWA chrome: standalone capable, dark ground under a
  // translucent status bar (globals.css already pads safe-area insets).
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "Usual",
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
  themeColor: "#121715",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ServiceWorkerProvider>{children}</ServiceWorkerProvider>
      </body>
    </html>
  );
}
