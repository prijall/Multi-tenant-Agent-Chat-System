"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { fetchMe, provisionProfile } from "@/lib/api";
import { authClient } from "@/lib/auth-client";
import { findTenant } from "@/lib/tenants";

export default function SignIn() {
  const { tenant } = useParams<{ tenant: string }>();
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPending(true);
    setError("");
    const values = new FormData(event.currentTarget);
    // The Neon client throws on a rejected login instead of returning `error`.
    const result = await authClient.signIn
      .email({
        email: String(values.get("email")),
        password: String(values.get("password")),
      })
      .catch((reason: Error) => ({ error: reason }));
    if (result.error) {
      setError("Invalid email or password.");
      setPending(false);
      return;
    }
    let me = await fetchMe();
    if (!me) {
      // A sign-up can succeed at the auth provider and still fail to link here, so retry it.
      const problem = await provisionProfile(tenant);
      if (problem) {
        await authClient.signOut();
        setError(problem);
        setPending(false);
        return;
      }
      me = await fetchMe();
    }
    if (me?.organization !== tenant) {
      await authClient.signOut();
      setError("This account belongs to a different organization.");
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
        <h1>Welcome back</h1>
        <form onSubmit={submit} className="auth-form">
          <label>Email<input name="email" type="email" autoComplete="email" required /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="button primary" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
        </form>
        <p className="alternate">New here? <Link href={`/a/${tenant}/sign-up`}>Create account</Link></p>
      </section>
    </main>
  );
}
