import type { Metadata } from "next";
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
      // Opened over the form (iOS's Safari sheet with Done, a new tab
      // elsewhere), as Settings' About rows are: in the installed app's
      // window the legal pages' only way back is the logo, and the typed
      // fields would be gone.
      legal={
        <>
          By creating an account you agree to the{" "}
          <a href="/terms" target="_blank" rel="noopener">
            Terms
          </a>{" "}
          and{" "}
          <a href="/privacy" target="_blank" rel="noopener">
            Privacy Policy
          </a>
          .
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
