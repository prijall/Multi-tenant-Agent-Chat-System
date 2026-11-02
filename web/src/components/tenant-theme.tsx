"use client";

import { type CSSProperties, type ReactNode } from "react";
import { useParams } from "next/navigation";

import { findTenant } from "@/lib/tenants";

/** Gives everything under an organization's URL that organization's colours and tab title. */
export function TenantTheme({ children }: { children: ReactNode }) {
  const { tenant: slug } = useParams<{ tenant: string }>();
  const tenant = findTenant(slug);

  const style = tenant && ({
    "--paper": tenant.theme.paper,
    "--surface": tenant.theme.surface,
    "--surface-alt": tenant.theme.surfaceAlt,
    "--ink": tenant.theme.ink,
    "--line": tenant.theme.line,
    "--accent": tenant.theme.accent,
    "--on-accent": tenant.theme.onAccent,
  } as CSSProperties);

  return <div className="tenant-theme" style={style}>{tenant && <title>{tenant.name}</title>}{children}</div>;
}
