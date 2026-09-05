"use client";

import { useActionState } from "react";
import { verifyEmailAction, type AuthFormState } from "@/app/auth/actions";

type Props = {
  title: string;
  submitLabel: string;
  action: (prev: AuthFormState, formData: FormData) => Promise<AuthFormState>;
  /** one line under the title — what the account being created gets */
  intro?: string;
  withName?: boolean;
  alt: { text: string; href: string; label: string };
};

export default function AuthForm({
  title,
  submitLabel,
  action,
  intro,
  withName,
  alt,
}: Props) {
  const [state, formAction, pending] = useActionState(action, null);

  if (state?.verify) return <VerifyEmail verify={state.verify} />;

  return (
    <main className="auth-wrap">
      <form className="auth-card" action={formAction}>
        <h1>{title}</h1>
        {intro ? <p className="subsheet-hint auth-intro">{intro}</p> : null}
        {withName ? (
          <label>
            Name
            <input name="name" type="text" autoComplete="name" required />
          </label>
        ) : null}
        <label>
          Email
          <input name="email" type="email" autoComplete="email" required />
        </label>
        <label>
          Password
          <input
            name="password"
            type="password"
            autoComplete={withName ? "new-password" : "current-password"}
            required
            minLength={8}
          />
        </label>
        {state?.error ? <p className="auth-error">{state.error}</p> : null}
        <button type="submit" disabled={pending}>
          {pending ? "…" : submitLabel}
        </button>
        <p className="auth-alt">
          {alt.text} <a href={alt.href}>{alt.label}</a>
        </p>
      </form>
    </main>
  );
}

type Verify = NonNullable<NonNullable<AuthFormState>["verify"]>;

/**
 * The one-time-code step. Sign-in lands here when the address is not yet
 * verified (a button sends the code); sign-up lands here with the code
 * already on its way. A correct code signs the account in.
 */
function VerifyEmail({ verify }: { verify: Verify }) {
  const [state, formAction, pending] = useActionState(verifyEmailAction, {
    verify,
  });
  const { email } = verify;
  const sent = state?.verify?.sent ?? verify.sent;

  return (
    <main className="auth-wrap">
      <form className="auth-card" action={formAction}>
        <h1>Verify your email</h1>
        <p className="subsheet-hint auth-intro">
          {sent
            ? `Enter the code we emailed to ${email}.`
            : `${email} hasn't been verified yet. We'll email you a code.`}
        </p>
        <input type="hidden" name="email" value={email} />
        {sent ? (
          <label>
            Verification code
            <input
              name="otp"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
            />
          </label>
        ) : null}
        {state?.error ? <p className="auth-error">{state.error}</p> : null}
        <button
          type="submit"
          name="intent"
          value={sent ? "verify" : "send"}
          disabled={pending}
        >
          {pending ? "…" : sent ? "Verify" : "Send verification code"}
        </button>
        {sent ? (
          <button
            type="submit"
            name="intent"
            value="send"
            className="auth-linkish"
            formNoValidate
            disabled={pending}
          >
            Send a new code
          </button>
        ) : null}
        <p className="auth-alt">
          <a href="/auth/sign-in">Back to sign in</a>
        </p>
      </form>
    </main>
  );
}
