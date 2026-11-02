"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { provisionProfile } from "@/lib/api";
import { authClient } from "@/lib/auth-client";
import { findTenant } from "@/lib/tenants";

export default function SignUp() {
  const { tenant } = useParams<{ tenant: string }>();
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const values = new FormData(event.currentTarget);
    // The Neon client throws on a rejected sign-up instead of returning `error`.
    const result = await authClient.signUp
      .email({
        name: String(values.get("name")),
        email: String(values.get("email")),
        password: String(values.get("password")),
      })
      .catch((reason: Error) => ({ error: reason }));
    if (result.error) {
      setError(result.error.message || "Could not create account.");
      setPending(false);
      return;
    }
    const problem = await provisionProfile(tenant);
    if (problem) {
      await authClient.signOut();
      setError(problem);
      setPending(false);
      return;
    }
    router.replace(`/a/${tenant}/workspace`);
  }

  return (
    <main className={`shell auth-shell ${tenant}`}>
      <Link className="back" href={`/a/${tenant}`}>← Back</Link>
      <section className="auth-card">
        <p className="eyebrow">{findTenant(tenant)?.name}</p>
        <h1>Create your account</h1>
        <form onSubmit={submit} className="auth-form">
          <label>Name<input name="name" autoComplete="name" required /></label>
          <label>Email<input name="email" type="email" autoComplete="email" required /></label>
          <label>Password<input name="password" type="password" minLength={8} autoComplete="new-password" required /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="button primary" disabled={pending}>{pending ? "Creating…" : "Create account"}</button>
        </form>
        <p className="alternate">Already registered? <Link href={`/a/${tenant}/sign-in`}>Sign in</Link></p>
      </section>
    </main>
  );
}
