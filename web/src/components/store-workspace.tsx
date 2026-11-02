"use client";

import { useEffect, useMemo, useState } from "react";
import {
  cancelOrder,
  confirmOrder,
  confirmReorder,
  fetchOrder,
  fetchOrders,
  fetchProducts,
  fetchReorderQuote,
  OrderQuote,
  OrderDetail,
  OrderSummary,
  Product,
  quoteOrder,
  updateOrder,
} from "@/lib/api";

export function StoreWorkspace() {
  const [products, setProducts] = useState<Product[]>([]);
  const [orders, setOrders] = useState<OrderSummary[]>([]);
  const [cart, setCart] = useState<Record<string, number>>({});
  const [quote, setQuote] = useState<OrderQuote | null>(null);
  const [reorderId, setReorderId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [selectedOrder, setSelectedOrder] = useState<OrderDetail | null>(null);
  const [address, setAddress] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    const [catalog, history] = await Promise.all([fetchProducts(), fetchOrders()]);
    setProducts(catalog);
    setOrders(history);
  }
  useEffect(() => { refresh().catch((error) => setMessage(error.message)); }, []);

  const count = useMemo(() => Object.values(cart).reduce((sum, value) => sum + value, 0), [cart]);
  function add(product: Product) {
    setCart((current) => ({ ...current, [product.sku]: (current[product.sku] || 0) + 1 }));
    setQuote(null);
  }
  async function preview() {
    setBusy(true); setMessage("");
    try {
      setReorderId(null);
      setQuote(await quoteOrder(Object.entries(cart).map(([sku, quantity]) => ({ sku, quantity }))));
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not quote order"); }
    finally { setBusy(false); }
  }
  async function place() {
    if (!quote) return;
    setBusy(true); setMessage("");
    try {
      if (reorderId) await confirmReorder(reorderId, quote.total);
      else if (editingId) await updateOrder(editingId, quote, address);
      else await confirmOrder(quote, address);
      setCart({}); setQuote(null); setReorderId(null); setEditingId(null); setAddress("");
      setMessage(editingId ? "Order updated successfully." : "Order created successfully."); await refresh();
    } catch (error) { setMessage(error instanceof Error ? error.message : "Could not place order"); }
    finally { setBusy(false); }
  }
  async function previewReorder(id: string) {
    setBusy(true); setMessage("");
    try { const next = await fetchReorderQuote(id); setQuote(next); setAddress(next.delivery_address || ""); setEditingId(null); setReorderId(id); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not prepare reorder"); }
    finally { setBusy(false); }
  }
  async function edit(id: string) {
    setBusy(true); setMessage("");
    try { const detail = await fetchOrder(id); const items = detail.items.map(({ sku, quantity }) => ({ sku, quantity })); setCart(Object.fromEntries(items.map((item) => [item.sku, item.quantity]))); setAddress(detail.delivery_address); setQuote(await quoteOrder(items)); setReorderId(null); setEditingId(id); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not edit order"); }
    finally { setBusy(false); }
  }
  async function track(id: string) { setBusy(true); try { setSelectedOrder(await fetchOrder(id)); } catch (error) { setMessage(error instanceof Error ? error.message : "Could not load order"); } finally { setBusy(false); } }
  async function cancel(id: string) {
    setBusy(true); setMessage("");
    try { await cancelOrder(id); setMessage("Order cancelled."); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not cancel order"); }
    finally { setBusy(false); }
  }

  return <div className="workspace-grid">
    <section><div className="section-heading"><div><p className="eyebrow">Live catalog</p><h2>Tea and teaware</h2></div><span>{count} in cart</span></div>
      <div className="product-grid">{products.map((product) => <article className="product-card" key={product.sku}><p className="eyebrow">{product.sku}</p><h3>{product.name}</h3><p>Rs {product.price} · {product.stock} available</p><button className="button" disabled={!product.stock || busy} onClick={() => add(product)}>{product.stock ? "Add" : "Out of stock"}</button></article>)}</div>
      <button className="button primary" disabled={!count || busy} onClick={preview}>Review order</button>
    </section>
    <aside className="workspace-aside">
      {message && <p className="notice">{message}</p>}
      {quote && <section className="summary-card"><p className="eyebrow">Confirm before saving</p><h2>{editingId ? "Update order" : reorderId ? "Order again" : "Order summary"}</h2>{quote.items.map((item) => <div className="summary-line" key={item.sku}><span>{item.quantity} × {item.name}</span><strong>Rs {item.line_total || Number(item.unit_price) * item.quantity}</strong></div>)}<div className="summary-total"><span>Total</span><strong>Rs {quote.total}</strong></div>{!reorderId && <label>Delivery address<textarea value={address} onChange={(event) => setAddress(event.target.value)} /></label>}<div className="actions"><button className="button primary" disabled={busy || !address.trim()} onClick={place}>Confirm {editingId ? "changes" : "order"}</button><button className="button" onClick={() => setQuote(null)}>Back</button></div></section>}
      {selectedOrder && <section className="summary-card"><p className="eyebrow">Order tracking</p><h2>{selectedOrder.status}</h2>{selectedOrder.history.map((event) => <div className="summary-line" key={`${event.status}-${event.at}`}><span>{event.status}</span><time>{new Date(event.at).toLocaleString()}</time></div>)}<button className="button" onClick={() => setSelectedOrder(null)}>Close</button></section>}
      <section><p className="eyebrow">Your history</p><h2>My orders</h2><div className="order-list">{orders.map((order) => <article className="order-card" key={order.id}><div><strong>Rs {order.total}</strong><p>{order.status}</p></div><div className="order-actions"><button onClick={() => track(order.id)}>Track</button>{order.status === "pending" && <><button onClick={() => edit(order.id)}>Edit</button><button onClick={() => cancel(order.id)}>Cancel</button></>}<button onClick={() => previewReorder(order.id)}>Order again</button></div></article>)}</div></section>
    </aside>
  </div>;
}
