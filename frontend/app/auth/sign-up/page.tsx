import AuthForm from "@/components/AuthForm";
import { signUpAction } from "@/app/auth/actions";

export default function SignUpPage() {
  return (
    <AuthForm
      title="Create your account"
      submitLabel="Sign up"
      action={signUpAction}
      withName
      alt={{
        text: "Already registered?",
        href: "/auth/sign-in",
        label: "Sign in",
      }}
    />
  );
}
