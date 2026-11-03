import Link from "next/link";

// Next's default 404 carries a global style that turns the body black in dark mode, and that
// style can outlive the 404 and darken pages reached by client-side navigation.
export default function NotFound() {
  return (
    <main className="shell auth-shell">
      <section className="auth-card">
        <h1>Page not found</h1>
        <p className="lede">This page doesn&apos;t exist.</p>
        <div className="actions"><Link className="button primary" href="/">Back to sign in</Link></div>
      </section>
    </main>
  );
}
