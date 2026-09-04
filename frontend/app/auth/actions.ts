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

export async function signUpAction(
  _prev: AuthFormState,
  formData: FormData,
): Promise<AuthFormState> {
  // Open sign-up (§8.9): any address may register. Which account is the
  // owner is the API's call (backend/auth.py, ALLOWED_EMAIL); every other
  // account is a demo, bounded by the quota rather than by this door.
  const { error } = await getAuth().signUp.email({
    name: String(formData.get("name") ?? ""),
    email: String(formData.get("email") ?? ""),
    password: String(formData.get("password") ?? ""),
  });
  if (error) return { error: error.message ?? "Sign-up failed" };
  redirect("/");
}
