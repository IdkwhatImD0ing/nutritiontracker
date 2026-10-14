"use client";

import Link from "next/link";
import { useActionState, useState, type FormEvent, type ReactNode } from "react";
import {
  resetPasswordAction,
  verifyEmailAction,
  type AuthFormState,
} from "@/app/auth/actions";
import { ChevronLeft } from "./icons";
import TallyMark from "./TallyMark";

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
  /** the submit button's label while the action runs ("Signing in…") */
  pendingLabel: string;
  action: (prev: AuthFormState, formData: FormData) => Promise<AuthFormState>;
  /** one line under the title — what the account being created gets */
  intro?: string;
  withName?: boolean;
  /** a line under the submit — what creating the account agrees to */
  legal?: ReactNode;
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
  legal,
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
      <AuthTop />
      <form className="auth-card" action={formAction} onSubmit={check} noValidate>
        <TallyMark className="auth-mark" />
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
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            defaultValue={state?.email}
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
        {withName ? null : (
          <a href="/auth/reset" className="auth-forgot">
            Forgot password?
          </a>
        )}
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
        {legal ? <p className="auth-alt auth-legal">{legal}</p> : null}
        <p className="auth-alt">
          {/* the other auth page: a different route, so the form mounts fresh */}
          {alt.text} <Link href={alt.href}>{alt.label}</Link>
        </p>
      </form>
    </main>
  );
}

/** The way out of the auth pages, which the installed app (no browser back)
 *  otherwise lacks. Not BackBar: its names are the signed-in tabs, and "/"
 *  is the landing page here, not Today. */
function AuthTop() {
  return (
    <div className="top-bar auth-top">
      <Link href="/" className="nav-back" aria-label="Back to the Tally start page">
        <ChevronLeft />
        Back
      </Link>
    </div>
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
      <AuthTop />
      <form className="auth-card" action={formAction}>
        <TallyMark className="auth-mark" />
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
            <input {...OTP_PROPS} enterKeyHint="done" />
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
          {/* a full load, not next/link: from /auth/sign-in a soft navigation
              to the same URL would keep this verify step on screen */}
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
  // the button that sent the form: only that one says it is working
  const [intent, setIntent] = useState<"reset" | "send" | null>(null);
  const sent = state?.sent ?? false;
  const primary = sent ? "reset" : "send";
  const primaryBusy = pending && intent === primary;
  const resendBusy = pending && sent && intent === "send";

  return (
    <main className="auth-wrap">
      <AuthTop />
      <form className="auth-card" action={formAction}>
        <TallyMark className="auth-mark" />
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
              {sent ? "Setting password…" : "Sending…"}
            </>
          ) : sent ? (
            "Set new password"
          ) : (
            "Email me a code"
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
