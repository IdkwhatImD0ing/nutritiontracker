"use client";

import { useActionState } from "react";
import type { AuthFormState } from "@/app/auth/actions";

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
