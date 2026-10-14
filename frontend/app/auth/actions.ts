"use server";

import { redirect } from "next/navigation";
import { getAuth } from "@/lib/auth/server";

export type AuthFormState = {
  error?: string;
  /**
   * Set when the account still has to prove it owns its address: the form
   * swaps to the one-time-code step. `sent` is whether a code is already on
   * its way.
   */
  verify?: { email: string; sent: boolean };
  /** a second code went out from the verify step's "Send a new code" */
  resent?: boolean;
} | null;

/**
 * App copy for the provider codes the SDK is known to send (its
 * normalizeBetterAuthError turns Better Auth's codes into these; an
 * unreachable auth server comes back as NETWORK_*). Any other code shows the
 * provider's own sentence. Sign-in maps its wrong password itself
 * (invalid_credentials): the SDK gives most 401s that code, so the code
 * step could meet it too.
 */
function authMessage(
  error: { code?: string; message?: string },
  fallback: string,
): string {
  const code = error.code ?? "";
  if (code.startsWith("NETWORK_")) {
    return "Couldn't reach the sign-in service. Check your connection and try again.";
  }
  switch (code) {
    case "user_already_exists":
    case "email_exists":
      return "There's already an account with that email. Sign in instead.";
    case "over_request_rate_limit":
    case "over_email_send_rate_limit":
      return "Too many tries. Wait a little, then try again.";
  }
  return error.message ?? fallback;
}

export async function signInAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const email = String(formData.get("email") ?? "");
  const { error } = await getAuth().signIn.email({
    email,
    password: String(formData.get("password") ?? ""),
  });
  // Neon Auth requires a verified address before it opens a session, but
  // sends nothing on its own — both Console "send verification" toggles are
  // off, so the app is the only sender and nobody gets two codes. Offer one.
  if (error?.code === "email_not_confirmed") {
    return { verify: { email, sent: false } };
  }
  if (error?.code === "invalid_credentials") {
    return { error: "That email and password don't match." };
  }
  if (error) return { error: authMessage(error, "Sign-in failed") };
  redirect("/");
}

export async function signUpAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  // Open sign-up (§8.9): any address may register. Which account is the
  // owner is the API's call (backend/auth.py, ALLOWED_EMAIL); every other
  // account is a demo, bounded by the quota rather than by this door.
  const email = String(formData.get("email") ?? "");
  const { data, error } = await getAuth().signUp.email({
    name: String(formData.get("name") ?? ""),
    email,
    password: String(formData.get("password") ?? ""),
  });
  if (error) return { error: authMessage(error, "Sign-up failed") };
  // With verification required the account exists but has no session yet:
  // send the code and go straight to the step that takes it.
  if (data?.user && !data.user.emailVerified) return sendCode(email);
  redirect("/");
}

/** The code step's one action: `intent=send` (re)sends, anything else verifies. */
export async function verifyEmailAction(
  prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const email = String(formData.get("email") ?? "");
  if (formData.get("intent") === "send") {
    const result = await sendCode(email);
    // Only a code after one already sent is "new": sign-in's first send on
    // an unverified address comes through here too.
    return prev?.verify?.sent && !result?.error
      ? { ...result, resent: true }
      : result;
  }
  const { data, error } = await getAuth().emailOtp.verifyEmail({
    email,
    otp: String(formData.get("otp") ?? ""),
  });
  if (error) {
    return {
      verify: { email, sent: true },
      error: authMessage(error, "Verification failed"),
    };
  }
  // auto_sign_in_after_verification is on, so the response carried the
  // session cookie. Were it off, the credentials form is the way in.
  redirect(data?.token ? "/" : "/auth/sign-in");
}

async function sendCode(email: string): Promise<AuthFormState> {
  const { error } = await getAuth().emailOtp.sendVerificationOtp({
    email,
    type: "email-verification",
  });
  if (error) {
    return {
      verify: { email, sent: false },
      error: authMessage(error, "Could not send the code"),
    };
  }
  return { verify: { email, sent: true } };
}
