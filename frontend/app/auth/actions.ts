"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { getAuth } from "@/lib/auth/server";

export type AuthFormState = {
  error?: string;
  /**
   * What was typed, handed back with an error: React resets the form once
   * its action settles, and these are the fields' defaults, so the reset
   * puts them back. Never the password.
   */
  email?: string;
  name?: string;
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
    return { error: "That email and password don't match.", email };
  }
  if (error) return { error: authMessage(error, "Sign-in failed"), email };
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
  const name = String(formData.get("name") ?? "");
  const { data, error } = await getAuth().signUp.email({
    name,
    email,
    password: String(formData.get("password") ?? ""),
  });
  if (error) return { error: authMessage(error, "Sign-up failed"), email, name };
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
    // an unverified address comes through here too. A failed resend keeps
    // the code step: the code already sent still works.
    if (!prev?.verify?.sent) return result;
    return result?.error
      ? { ...result, verify: { email, sent: true } }
      : { ...result, resent: true };
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

/** The reset step's state: `sent` once a code is on its way, and the form
 *  then asks for it and the new password; `resent` when a second code went
 *  out from "Send a new code". */
export type ResetFormState = {
  email: string;
  sent: boolean;
  resent?: boolean;
  error?: string;
} | null;

/**
 * A forgotten password (/auth/reset; Bill, 2026-10-13), in the verify step's
 * shape: `intent=send` (re)sends a `forget-password` code, anything else sets
 * the new password with it and signs in. The code goes out the way the
 * verification code does — the app asks for it, so the Console send toggles
 * (sign-up and sign-in verification only) stay off.
 */
export async function resetPasswordAction(
  prev: ResetFormState,
  formData: FormData,
): Promise<ResetFormState> {
  const email = String(formData.get("email") ?? "");
  if (formData.get("intent") === "send") {
    // An address with no account gets the same success and no email, so
    // this step tells no one who has an account.
    const { error } = await getAuth().emailOtp.sendVerificationOtp({
      email,
      type: "forget-password",
    });
    // A failed resend keeps the code step: the code already sent still works.
    const sent = !!prev?.sent;
    if (error) {
      return { email, sent, error: authMessage(error, "Could not send the code") };
    }
    return { email, sent: true, resent: sent };
  }
  const password = String(formData.get("password") ?? "");
  const error = await resetWithCode(email, String(formData.get("otp") ?? ""), password);
  if (error) return { email, sent: true, error };
  // A reset opens no session; the password it just set does. Should that
  // fail, the credentials form is the way in.
  const signIn = await getAuth().signIn.email({ email, password });
  redirect(signIn.error ? "/auth/sign-in" : "/");
}

/**
 * POST email-otp/reset-password `{email, otp, password}` (Neon's Email OTP
 * guide; Better Auth's email-otp plugin). Not via
 * `getAuth().emailOtp.resetPassword`: in @neondatabase/auth 0.5.0-beta that
 * posts to `email-otp/passcode`, a route Better Auth does not serve. So the
 * request goes through the SDK's own proxy handler — the one /api/auth
 * mounts, and the way the browser client would send it. Returns the error
 * message, or null once the password is set. The reply is Better Auth's own,
 * which the SDK's error mapping never sees, so its codes get app copy here.
 */
async function resetWithCode(
  email: string,
  otp: string,
  password: string,
): Promise<string | null> {
  const h = await headers();
  const origin = h.get("origin") ?? `https://${h.get("host")}`;
  const path = ["email-otp", "reset-password"];
  const res = await getAuth()
    .handler()
    .POST(
      new Request(`${origin}/api/auth/${path.join("/")}`, {
        method: "POST",
        headers: { "content-type": "application/json", origin },
        body: JSON.stringify({ email, otp, password }),
      }),
      { params: Promise.resolve({ path }) },
    );
  if (res.ok) return null;
  // its rate limiter's 429 carries a sentence and no code
  if (res.status === 429) return "Too many tries. Wait a little, then try again.";
  const body = await res.json().catch(() => null);
  switch (body?.code) {
    case "INVALID_OTP":
      return "That code isn't right. Check the email and try again.";
    case "OTP_EXPIRED":
      return "That code has expired. Send a new code.";
    case "TOO_MANY_ATTEMPTS":
      return "Too many wrong tries for that code. Send a new code.";
  }
  return body?.message ?? "Could not reset the password";
}
