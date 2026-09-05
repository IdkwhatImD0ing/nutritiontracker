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
  if (error) return { error: error.message ?? "Sign-in failed" };
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
  if (error) return { error: error.message ?? "Sign-up failed" };
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
