"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { fetchMe } from "@/lib/api";
import { authClient } from "@/lib/auth-client";

/** One plain login: the account's stored organization decides where it lands, never this page. */
export function PlatformSignIn() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    fetchMe().then((me) => {
      if (me) router.replace(`/a/${me.organization}/workspace`);
    });
  }, [router]);

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
    const me = await fetchMe();
    if (!me) {
      await authClient.signOut();
      setError("This login isn't linked to an organization yet. Create your account from your organization's link below.");
      setPending(false);
      return;
    }
    router.replace(`/a/${me.organization}/workspace`);
  }

  return (
    <main className="shell auth-shell">
      <section className="auth-card">
        <h1>Sign in</h1>
        <form onSubmit={submit} className="auth-form">
          <label>Email<input name="email" type="email" autoComplete="email" required /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
          {error && <p className="error" role="alert">{error}</p>}
          <button className="button primary" disabled={pending}>{pending ? "Signing in…" : "Sign in"}</button>
        </form>
      </section>
    </main>
  );
}
