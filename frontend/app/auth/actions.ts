"use server";

import { redirect } from "next/navigation";
import { getAuth } from "@/lib/auth/server";

export type AuthFormState = { error: string } | null;

export async function signInAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const { error } = await getAuth().signIn.email({
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  });
  if (error) return { error: error.message ?? "Sign-in failed" };
  redirect("/");
}

const ALLOWED_EMAIL = process.env.ALLOWED_EMAIL ?? "billzhangsc@gmail.com";

export async function signUpAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  const email = String(formData.get("email") ?? "");
  // Single-user allowlist (§2): registration exists only for the one first-run
  // sign-up; the API also rejects any token for a different email.
  if (email.trim().toLowerCase() !== ALLOWED_EMAIL.trim().toLowerCase()) {
    return { error: "Sign-up is restricted on this deployment" };
  }
  const { error } = await getAuth().signUp.email({
    name: String(formData.get("name") ?? ""),
    email,
    password: String(formData.get("password") ?? ""),
  });
  if (error) return { error: error.message ?? "Sign-up failed" };
  redirect("/");
}
