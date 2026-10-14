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
} | null;

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
  if (error) return { error: error.message ?? "Sign-in failed", email };
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
  if (error) return { error: error.message ?? "Sign-up failed", email, name };
  // With verification required the account exists but has no session yet:
  // send the code and go straight to the step that takes it.
  if (data?.user && !data.user.emailVerified) return sendCode(email);
  redirect("/");
}

/** The code step's one action: `intent=send` (re)sends, anything else verifies. */
export async function verifyEmailAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const email = String(formData.get("email") ?? "");
  if (formData.get("intent") === "send") return sendCode(email);
  const { data, error } = await getAuth().emailOtp.verifyEmail({
    email,
    otp: String(formData.get("otp") ?? ""),
  });
  if (error) {
    return {
      verify: { email, sent: true },
      error: error.message ?? "Verification failed",
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
      error: error.message ?? "Could not send the code",
    };
  }
  return { verify: { email, sent: true } };
}

/** The reset step's state: `sent` once a code is on its way, and the form
 *  then asks for it and the new password. */
export type ResetFormState = { email: string; sent: boolean; error?: string } | null;

/**
 * A forgotten password (/auth/reset; Bill, 2026-10-13), in the verify step's
 * shape: `intent=send` (re)sends a `forget-password` code, anything else sets
 * the new password with it and signs in. The code goes out the way the
 * verification code does — the app asks for it, so the Console send toggles
 * (sign-up and sign-in verification only) stay off.
 */
export async function resetPasswordAction(
  _prev: ResetFormState,
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
    if (error) {
      return { email, sent: false, error: error.message ?? "Could not send the code" };
    }
    return { email, sent: true };
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
 * message, or null once the password is set.
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
  const body = await res.json().catch(() => null);
  return body?.message ?? "Could not reset the password";
}
