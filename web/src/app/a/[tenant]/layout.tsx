import { Suspense } from "react";
import { TenantTheme } from "@/components/tenant-theme";
import { tenants } from "@/lib/tenants";

// Known organisations prerender; unknown slugs are turned away in src/proxy.ts.
export function generateStaticParams() {
  return tenants.map((tenant) => ({ tenant: tenant.slug }));
}

export default function TenantLayout({ children }: { children: React.ReactNode }) {
  return <Suspense fallback={<main className="shell"><p className="lede">Loading…</p></main>}><TenantTheme>{children}</TenantTheme></Suspense>;
}
