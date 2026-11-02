"use client";

import { useEffect, useState } from "react";

import { fetchBookings, fetchOrders } from "@/lib/api";
import type { Tenant } from "@/lib/tenants";

type ActivityRow = { id: string; title: string; detail: string; status: string };

async function loadRows(tenant: Tenant): Promise<ActivityRow[]> {
  const options = { timeZone: tenant.timezone, dateStyle: "medium" } as const;
  if (tenant.kind === "store") {
    const date = new Intl.DateTimeFormat(undefined, options);
    return (await fetchOrders()).map((order) => ({
      id: order.id,
      title: `Rs ${order.total}`,
      detail: date.format(new Date(order.created_at)),
      status: order.status,
    }));
  }
  const dateTime = new Intl.DateTimeFormat(undefined, { ...options, timeStyle: "short" });
  return (await fetchBookings()).map((booking) => ({
    id: booking.id,
    title: booking.service,
    detail: `${dateTime.format(new Date(booking.starts_at))} · ${booking.practitioner}`,
    status: booking.status,
  }));
}

/** Read-only, always from the same API the agent's tools read, so the two can't disagree. */
export function MyActivity({ tenant, version }: { tenant: Tenant; version: number }) {
  const [rows, setRows] = useState<ActivityRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    loadRows(tenant)
      .then((next) => {
        setRows(next);
        setError("");
      })
      .catch((reason) => setError(reason.message));
  }, [tenant, version]);

  const label = tenant.kind === "store" ? "orders" : "bookings";
  return (
    <details className="my-activity" open>
      <summary>My {label}</summary>
      {error && <p className="activity-note" role="alert">{error}</p>}
      {!error && !rows && <p className="activity-note">Loading…</p>}
      {rows?.length === 0 && <p className="activity-note">No {label} yet.</p>}
      <ul>
        {rows?.map((row) => (
          <li key={row.id}>
            <strong>{row.title}</strong>
            <span>{row.detail}</span>
            <em className={`activity-status ${row.status}`}>{row.status}</em>
          </li>
        ))}
      </ul>
    </details>
  );
}
