"use client";

import Link from "next/link";
import { notFound, useParams } from "next/navigation";

import { findTenant } from "@/lib/tenants";

export default function TenantEntry() {
  const { tenant: slug } = useParams<{ tenant: string }>();
  const tenant = findTenant(slug);
  if (!tenant) notFound();

  return (
    <main className={`shell ${tenant.slug}`}>
      <section className="hero-card">
        <div className="tenant-mark" aria-hidden="true">{tenant.name.at(0)}</div>
        <p className="eyebrow">{tenant.description}</p>
        <h1>{tenant.name}</h1>
        <p className="lede">{tenant.greeting}</p>
        <div className="actions">
          <Link className="button primary" href={`/a/${tenant.slug}/sign-in`}>Sign in</Link>
          <Link className="button" href={`/a/${tenant.slug}/sign-up`}>Create account</Link>
        </div>
      </section>
    </main>
  );
}
