import type { Metadata } from "next";
import Link from "next/link";
import AuthForm from "@/components/AuthForm";
import { signUpAction } from "@/app/auth/actions";

export const metadata: Metadata = {
  title: "Create account",
  robots: { index: false, follow: false },
};

export default function SignUpPage() {
  return (
    <AuthForm
      title="Create a demo account"
      intro="New accounts are demos: one log a day — by voice, photo, barcode, or search — in a diary only you can see."
      submitLabel="Create account"
      pendingLabel="Creating account…"
      action={signUpAction}
      withName
      legal={
        <>
          By creating an account you agree to the <Link href="/terms">Terms</Link>{" "}
          and <Link href="/privacy">Privacy Policy</Link>.
        </>
      }
      alt={{
        text: "Already registered?",
        href: "/auth/sign-in",
        label: "Sign in",
      }}
    />
  );
}
