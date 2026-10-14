import type { Metadata } from "next";
import AuthForm from "@/components/AuthForm";
import { signInAction } from "@/app/auth/actions";

// Nothing to index on a credential form, and it competes with `/` for the
// brand query if left crawlable.
export const metadata: Metadata = {
  title: "Sign in",
  robots: { index: false, follow: false },
};

export default function SignInPage() {
  return (
    <AuthForm
      title="Sign in to Tally"
      submitLabel="Sign in"
      action={signInAction}
      alt={{
        text: "No account?",
        href: "/auth/sign-up",
        label: "Create a demo account",
      }}
    />
  );
}
