import AuthForm from "@/components/AuthForm";
import { signInAction } from "@/app/auth/actions";

export default function SignInPage() {
  return (
    <AuthForm
      title="Sign in to Tally"
      submitLabel="Sign in"
      action={signInAction}
      alt={{
        text: "No account?",
        href: "/auth/sign-up",
        label: "Sign up for a demo",
      }}
    />
  );
}
