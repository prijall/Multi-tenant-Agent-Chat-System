"use client";

import { useEffect, useState } from "react";

import { type AdminOrder, advanceOrderStatus, fetchAdminOrders } from "@/lib/api";

/** Moves mock orders through their life cycle, so tracking and edit rules can be demonstrated. */
export function AdminOrders() {
  const [orders, setOrders] = useState<AdminOrder[] | null>(null);
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    fetchAdminOrders().then(setOrders).catch((reason) => setError(reason.message));
  }, []);

  async function advance(order: AdminOrder) {
    if (!order.next_status) return;
    setBusyId(order.id);
    setError("");
    try {
      await advanceOrderStatus(order.id, order.next_status);
      setOrders(await fetchAdminOrders());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update the order");
    } finally {
      setBusyId("");
    }
  }

  return (
    <section className="admin-orders">
      <h2>Orders</h2>
      {error && <p className="error" role="alert">{error}</p>}
      {!error && !orders && <p>Loading orders…</p>}
      {orders?.length === 0 && <p>No orders yet.</p>}
      <div className="member-list">
        {orders?.map((order) => (
          <article className="member-row" key={order.id}>
            <div>
              <strong>Rs {order.total}</strong>
              <span>{order.customer}</span>
              <span className="status">{order.status}</span>
            </div>
            {order.next_status && (
              <button className="button" disabled={busyId === order.id} onClick={() => advance(order)}>
                Mark {order.next_status}
              </button>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
