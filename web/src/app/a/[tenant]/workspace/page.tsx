"use client";
import { useEffect, useState } from "react";
import { notFound, useParams, useRouter } from "next/navigation";
import { fetchMe, type Me } from "@/lib/api";
import { authClient } from "@/lib/auth-client";
import { findTenant } from "@/lib/tenants";
import { AdminDashboard } from "@/components/admin-dashboard";
import { ChatWorkspace } from "@/components/chat-workspace";

export default function Workspace() {
  const { tenant: slug } = useParams<{ tenant: string }>();
  const tenant = findTenant(slug);
  const router = useRouter();
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    fetchMe().then((result) => {
      if (!result) router.replace("/");
      // A user who edits the URL to another org lands back in their own workspace.
      else if (result.organization !== slug) router.replace(`/a/${result.organization}/workspace`);
      else setMe(result);
    });
  }, [slug, router]);

  async function signOut() {
    await authClient.signOut();
    router.push("/");
  }

  if (!tenant) notFound();
  if (!me) return <main className="shell"><p className="lede">Loading…</p></main>;

  return (
    <div className="app-shell">
      <header className="app-bar">
        <div className="brand">
          <div className="tenant-mark" aria-hidden="true">{tenant.name.at(0)}</div>
          <div>
            <strong>{tenant.name}</strong>
            <span>{me.role === "admin" ? "Admin dashboard" : tenant.agentName}</span>
          </div>
        </div>
        <button className="button" onClick={signOut}>Sign out</button>
      </header>
      {me.role === "admin"
        ? <main className="admin-main"><AdminDashboard tenant={slug} /></main>
        : <ChatWorkspace tenant={tenant} />}
    </div>
  );
}
