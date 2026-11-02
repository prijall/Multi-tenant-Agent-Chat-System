"use client";

import { useEffect, useState } from "react";

import { AdminOrders } from "@/components/admin-orders";
import { UserManagement } from "@/components/user-management";
import { type Analytics, fetchAnalytics } from "@/lib/api";

export function AdminDashboard({ tenant }: { tenant: string }) {
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const to = new Date();
    const from = new Date();
    from.setDate(from.getDate() - 30);
    fetchAnalytics(from, to).then(setData).catch((reason) => setError(reason.message));
  }, []);

  return (
    <section>
      {error && <p className="error">{error}</p>}
      {!data ? <p className="lede">Loading analytics…</p> : (
        <>
          <p className="lede">Last 30 days · {new Date(data.window.from).toLocaleDateString()}–{new Date(data.window.to).toLocaleDateString()}</p>
          <div className="metric-grid">
            <article><span>New profiles</span><strong>{data.new_customers}</strong></article>
            {tenant === "himali-tea" ? <><article><span>Order value</span><strong>Rs {data.order_value}</strong></article><article><span>Average order</span><strong>Rs {data.average_order_value}</strong></article></> : <article><span>Total bookings</span><strong>{data.bookings.reduce((sum, row) => sum + row.count, 0)}</strong></article>}
          </div>
          <div className="workspace-grid">
            <section className="summary-card"><h2>{tenant === "himali-tea" ? "Orders by status" : "Bookings by status"}</h2>{(tenant === "himali-tea" ? data.orders : data.bookings).map((row) => <div className="summary-line" key={row.status}><span>{row.status}</span><strong>{row.count}</strong></div>)}</section>
            {tenant === "lotus-wellness" && <section className="summary-card"><h2>Practitioner activity</h2>{data.practitioners.map((row) => <div className="summary-line" key={row.name}><span>{row.name}</span><strong>{row.booking_count}</strong></div>)}</section>}
          </div>
          <details className="definitions"><summary>Metric definitions</summary>{Object.entries(data.definitions).map(([name, definition]) => <p key={name}><strong>{name.replaceAll("_", " ")}:</strong> {definition}</p>)}</details>
        </>
      )}
      {tenant === "himali-tea" && <AdminOrders />}
      <UserManagement />
    </section>
  );
}
