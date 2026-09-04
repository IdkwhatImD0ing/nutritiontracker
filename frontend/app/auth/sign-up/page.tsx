import AuthForm from "@/components/AuthForm";
import { signUpAction } from "@/app/auth/actions";

export default function SignUpPage() {
  return (
    <AuthForm
      title="Create your account"
      intro="New accounts are demos: one log a day — by voice, photo, barcode, or search — in a diary only you can see."
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
