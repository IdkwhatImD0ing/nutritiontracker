"use client";

import { useActionState, useState, type FormEvent } from "react";
import { verifyEmailAction, type AuthFormState } from "@/app/auth/actions";

type Props = {
  title: string;
  submitLabel: string;
  /** the submit button's label while the action runs ("Signing in…") */
  pendingLabel: string;
  action: (prev: AuthFormState, formData: FormData) => Promise<AuthFormState>;
  /** one line under the title — what the account being created gets */
  intro?: string;
  withName?: boolean;
  alt: { text: string; href: string; label: string };
};

type Problem = { field: "name" | "email" | "password"; text: string };

/** What the browser's validation bubbles used to say, in the card's own
 *  error line. Sign-in only needs a password; sign-up holds it to 8. */
function checkFields(data: FormData, signUp: boolean): Problem | null {
  const name = String(data.get("name") ?? "").trim();
  const email = String(data.get("email") ?? "").trim();
  const password = String(data.get("password") ?? "");
  if (signUp && !name) return { field: "name", text: "Enter your name." };
  if (!email) return { field: "email", text: "Enter your email address." };
  if (!email.includes("@")) {
    return { field: "email", text: "That email address is missing an @." };
  }
  if (!password) return { field: "password", text: "Enter your password." };
  if (signUp && password.length < 8) {
    return { field: "password", text: "Your password needs at least 8 characters." };
  }
  return null;
}

export default function AuthForm({
  title,
  submitLabel,
  pendingLabel,
  action,
  intro,
  withName,
  alt,
}: Props) {
  const [state, formAction, pending] = useActionState(action, null);
  const [problem, setProblem] = useState<Problem | null>(null);

  if (state?.verify) return <VerifyEmail verify={state.verify} />;

  // Checked before the action runs: a refused submit never reaches the
  // server, and React doesn't reset a form whose action never ran, so the
  // fields keep what was typed.
  const check = (e: FormEvent<HTMLFormElement>) => {
    const found = checkFields(new FormData(e.currentTarget), !!withName);
    setProblem(found);
    if (!found) return;
    e.preventDefault();
    const field = e.currentTarget.elements.namedItem(found.field);
    if (field instanceof HTMLInputElement) field.focus();
  };
  const invalid = (field: Problem["field"]) => problem?.field === field || undefined;

  return (
    <main className="auth-wrap">
      <form className="auth-card" action={formAction} onSubmit={check} noValidate>
        <h1>{title}</h1>
        {intro ? <p className="subsheet-hint auth-intro">{intro}</p> : null}
        {withName ? (
          <label>
            Name
            <input
              name="name"
              type="text"
              autoComplete="name"
              required
              aria-invalid={invalid("name")}
            />
          </label>
        ) : null}
        <label>
          Email
          <input
            name="email"
            type="email"
            autoComplete="email"
            required
            aria-invalid={invalid("email")}
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
            aria-invalid={invalid("password")}
          />
          {withName ? (
            <span className="subsheet-hint">At least 8 characters</span>
          ) : null}
        </label>
        {/* always there, so the line is held (no jump) and a new error is
            announced. A server error is about the last try: not shown while
            the next one runs. */}
        <p className="auth-error" role="alert">
          {problem?.text ?? (pending ? null : state?.error)}
        </p>
        <button type="submit" disabled={pending} aria-busy={pending}>
          {pending ? (
            <>
              <span className="spinner on-accent" />
              {pendingLabel}
            </>
          ) : (
            submitLabel
          )}
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
  // the button that sent the form: only that one says it is working
  const [intent, setIntent] = useState<"verify" | "send" | null>(null);
  const { email } = verify;
  const sent = state?.verify?.sent ?? verify.sent;
  const primary = sent ? "verify" : "send";
  const primaryBusy = pending && intent === primary;
  const resendBusy = pending && sent && intent === "send";

  return (
    <main className="auth-wrap">
      <form className="auth-card" action={formAction}>
        <h1>Verify your email</h1>
        <p className="subsheet-hint auth-intro" aria-live="polite">
          {sent
            ? state?.resent
              ? `We sent a new code to ${email}.`
              : `Enter the code we emailed to ${email}.`
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
        <p className="auth-error" role="alert">
          {state?.error}
        </p>
        <button
          type="submit"
          name="intent"
          value={primary}
          disabled={pending}
          aria-busy={primaryBusy}
          onClick={() => setIntent(primary)}
        >
          {primaryBusy ? (
            <>
              <span className="spinner on-accent" />
              {sent ? "Verifying…" : "Sending…"}
            </>
          ) : sent ? (
            "Verify"
          ) : (
            "Send verification code"
          )}
        </button>
        {sent ? (
          <button
            type="submit"
            name="intent"
            value="send"
            className="auth-linkish"
            formNoValidate
            disabled={pending}
            aria-busy={resendBusy}
            onClick={() => setIntent("send")}
          >
            {resendBusy ? (
              <>
                <span className="spinner" />
                Sending a new code…
              </>
            ) : (
              "Send a new code"
            )}
          </button>
        ) : null}
        <p className="auth-alt">
          <a href="/auth/sign-in">Back to sign in</a>
        </p>
      </form>
    </main>
  );
}
