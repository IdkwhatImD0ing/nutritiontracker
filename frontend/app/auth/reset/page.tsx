import type { Metadata } from "next";
import { ResetPassword } from "@/components/AuthForm";

// Public like the other auth pages (proxy.ts leaves /auth alone), and as
// unindexed.
export const metadata: Metadata = {
  title: "Reset password",
  robots: { index: false, follow: false },
};

export default function ResetPage() {
  return <ResetPassword />;
}
