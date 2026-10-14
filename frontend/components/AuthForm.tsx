"use client";

import { useActionState } from "react";
import {
  resetPasswordAction,
  verifyEmailAction,
  type AuthFormState,
} from "@/app/auth/actions";

/** An emailed one-time code: six digits (Better Auth's default length, and
 *  the one Neon's emails carry), big and spaced, focused on arrival.
 *  autoFocus raises no iOS keyboard here — the step mounts after an action,
 *  not a tap — but the caret is already in the field. */
const OTP_PROPS = {
  name: "otp",
  type: "text",
  inputMode: "numeric",
  autoComplete: "one-time-code",
  pattern: "[0-9]*",
  maxLength: 6,
  autoFocus: true,
  className: "auth-otp",
  required: true,
} as const;

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
            <input
              name="name"
              type="text"
              autoComplete="name"
              autoCapitalize="words"
              defaultValue={state?.name}
              required
            />
          </label>
        ) : null}
        <label>
          Email
          <input
            name="email"
            type="email"
            autoComplete="email"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            defaultValue={state?.email}
            required
          />
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
        {withName ? null : (
          <a href="/auth/reset" className="auth-forgot">
            Forgot password?
          </a>
        )}
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
            <input {...OTP_PROPS} enterKeyHint="done" />
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

/**
 * A forgotten password (/auth/reset), in the verify step's shape: an email
 * gets a code, then the code and a new password set it and sign the account
 * in. Whether the address has an account is never said.
 */
export function ResetPassword() {
  const [state, formAction, pending] = useActionState(resetPasswordAction, null);
  const sent = state?.sent ?? false;

  return (
    <main className="auth-wrap">
      <form className="auth-card" action={formAction}>
        <h1>Reset your password</h1>
        <p className="subsheet-hint auth-intro">
          {sent
            ? `If ${state?.email} has an account, we've emailed it a code. Enter it with a new password.`
            : "We'll email you a code to set a new one."}
        </p>
        {sent ? (
          <>
            <input type="hidden" name="email" value={state?.email} />
            <label>
              Code
              <input {...OTP_PROPS} enterKeyHint="next" />
            </label>
            <label>
              New password
              <input
                name="password"
                type="password"
                autoComplete="new-password"
                required
                minLength={8}
              />
            </label>
          </>
        ) : (
          <label>
            Email
            <input
              name="email"
              type="email"
              autoComplete="email"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              defaultValue={state?.email}
              required
            />
          </label>
        )}
        {state?.error ? <p className="auth-error">{state.error}</p> : null}
        <button
          type="submit"
          name="intent"
          value={sent ? "reset" : "send"}
          disabled={pending}
        >
          {pending ? "…" : sent ? "Set new password" : "Email me a code"}
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
