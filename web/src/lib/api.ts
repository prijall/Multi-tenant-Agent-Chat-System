import { authClient } from "@/lib/auth-client";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type Me = { organization: string; role: "customer" | "admin" };
export type Membership = {
  id: string;
  email: string;
  role: "customer" | "admin";
  status: "pending" | "active";
};
export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  cards?: ChatCard[];
  created_at: string;
  actions: ChatAction[];
};
export type SlotOption = { label: string; practitioner: string; practitioner_id: string; starts_at: string };
export type SlotsCard = {
  type: "slots";
  title: string;
  service_id: string;
  booking_id: string | null;
  slots: SlotOption[];
};
export type StatusCard = {
  type: "status";
  title: string;
  status?: string;
  rows: { label: string; value?: string; status?: string }[];
  total?: string;
  timeline?: { status: string; at: string }[];
};
export type ChatCard = SlotsCard | StatusCard;
export type Citation = { document: string; section: string; content?: string };
export type AgentEvent =
  | { type: "text"; delta: string }
  | { type: "text_reset" }
  | { type: "activity"; label: string }
  | { type: "done"; message: ChatMessage }
  | { type: "error"; detail: string };
export type AdminOrder = {
  id: string;
  customer: string;
  status: string;
  total: string;
  created_at: string;
  next_status: string | null;
};
export type ChatAction = {
  id: string;
  type: string;
  status: "pending" | "confirmed" | "cancelled" | "expired";
  summary: {
    title: string;
    lines: { label: string; value: string }[];
    total?: string;
    note?: string;
  };
};
export type ActionResult = { message: ChatMessage; action: ChatAction };
export type Product = { sku: string; name: string; price: string; stock: number };
export type OrderSummary = { id: string; status: string; total: string; created_at: string };
export type OrderLine = {
  sku: string;
  name: string;
  quantity: number;
  unit_price: string;
  line_total?: string;
};
export type OrderQuote = {
  items: OrderLine[];
  total: string;
  delivery_address?: string;
};
export type OrderDetail = {
  id: string; status: string; total: string; delivery_address: string; payment_method: string;
  created_at: string; items: OrderLine[]; history: { status: string; at: string }[];
};
export type Service = { id: string; code: string; name: string; category: string; duration_minutes: number; price: string };
export type Practitioner = { id: string; code: string; name: string };
export type Slot = { practitioner_id: string; practitioner: string; starts_at: string; ends_at: string; local_start: string };
export type Booking = { id: string; service_id: string; service: string; practitioner_id: string; practitioner: string; starts_at: string; local_start: string; status: string; price: string };
export type BookingQuote = { service_id: string; service: string; practitioner_id: string; practitioner: string; starts_at: string; ends_at: string; timezone: string; duration_minutes: number; price: string };
export type Analytics = {
  window: { from: string; to: string };
  definitions: Record<string, string>;
  orders: { status: string; count: number; value: string }[];
  order_value: string; average_order_value: string;
  bookings: { status: string; count: number }[];
  practitioners: { name: string; booking_count: number }[];
  new_customers: number;
};

/** Calls the backend with the signed-in user's token; null when there is no session. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response | null> {
  // Signed out, the token call rejects with 401 rather than returning an empty result.
  const token = await authClient
    .token()
    .then((result) => result.data?.token)
    .catch(() => undefined);
  if (!token) return null;
  return fetch(`${API_URL}${path}`, {
    ...init,
    headers: { ...init.headers, Authorization: `Bearer ${token}` },
  });
}

/** The org and role the backend has on record for this account, never the URL's. */
export async function fetchMe(): Promise<Me | null> {
  const response = await apiFetch("/api/me");
  if (!response?.ok) return null;
  return response.json();
}

/** Links this login to the organization of the entry point; returns an error message on failure. */
export async function provisionProfile(tenant: string): Promise<string | null> {
  const response = await apiFetch(`/api/provision/${tenant}`, { method: "POST" });
  if (!response) return "Account created. Verify your email, then sign in to continue.";
  if (response.ok) return null;
  const body = await response.json().catch(() => ({}));
  return typeof body.detail === "string" ? body.detail : "Could not create the organization profile.";
}

async function authenticatedJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await apiFetch(path, init);
  if (!response) throw new Error("You are signed out");
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Request failed");
  }
  return response.json();
}

export const fetchProducts = () => authenticatedJson<Product[]>("/api/products");
export const fetchOrders = () => authenticatedJson<OrderSummary[]>("/api/orders");
export const fetchOrder = (id: string) => authenticatedJson<OrderDetail>(`/api/orders/${id}`);

export function quoteOrder(items: { sku: string; quantity: number }[]) {
  return authenticatedJson<OrderQuote>("/api/order-quotes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
}

export function confirmOrder(quote: OrderQuote, deliveryAddress: string) {
  return authenticatedJson("/api/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      items: quote.items.map(({ sku, quantity }) => ({ sku, quantity })),
      delivery_address: deliveryAddress,
      expected_total: quote.total,
    }),
  });
}

export function updateOrder(id: string, quote: OrderQuote, deliveryAddress: string) {
  return authenticatedJson(`/api/orders/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      items: quote.items.map(({ sku, quantity }) => ({ sku, quantity })),
      delivery_address: deliveryAddress,
      expected_total: quote.total,
    }),
  });
}

export function cancelOrder(id: string) {
  return authenticatedJson(`/api/orders/${id}`, { method: "DELETE" });
}

export function fetchReorderQuote(id: string) {
  return authenticatedJson<OrderQuote>(`/api/orders/${id}/reorder-quote`);
}

export function confirmReorder(id: string, total: string) {
  return authenticatedJson(`/api/orders/${id}/reorder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_total: total }),
  });
}

export const fetchServices = () => authenticatedJson<Service[]>("/api/services");
export const fetchBookings = () => authenticatedJson<Booking[]>("/api/bookings");
export function fetchPractitioners(serviceId: string) {
  return authenticatedJson<Practitioner[]>(`/api/practitioners?service_id=${serviceId}`);
}
export function fetchAvailability(serviceId: string, from: string, to: string, practitionerId?: string) {
  const query = new URLSearchParams({ service_id: serviceId, date_from: from, date_to: to });
  if (practitionerId) query.set("practitioner_id", practitionerId);
  return authenticatedJson<Slot[]>(`/api/availability?${query}`);
}
export function quoteBooking(serviceId: string, practitionerId: string, startsAt: string) {
  return authenticatedJson<BookingQuote>("/api/booking-quotes", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ service_id: serviceId, practitioner_id: practitionerId, starts_at: startsAt }),
  });
}
export function confirmBooking(quote: BookingQuote) {
  return authenticatedJson("/api/bookings", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ service_id: quote.service_id, practitioner_id: quote.practitioner_id, starts_at: quote.starts_at, expected_price: quote.price }),
  });
}
export function cancelBooking(id: string) {
  return authenticatedJson(`/api/bookings/${id}`, { method: "DELETE" });
}
export function rescheduleBooking(id: string, startsAt: string) {
  return authenticatedJson(`/api/bookings/${id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ starts_at: startsAt }),
  });
}
export function fetchAnalytics(from: Date, to: Date) {
  const query = new URLSearchParams({ date_from: from.toISOString(), date_to: to.toISOString() });
  return authenticatedJson<Analytics>(`/api/admin/analytics?${query}`);
}
export const fetchUsers = () => authenticatedJson<Membership[]>("/api/admin/users");
export function createUser(email: string, role: Membership["role"]) {
  return authenticatedJson<Membership>("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, role }),
  });
}
export function updateUserRole(id: string, role: Membership["role"]) {
  return authenticatedJson<Membership>(`/api/admin/users/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ role }),
  });
}
export function deleteUser(id: string) {
  return authenticatedJson<void>(`/api/admin/users/${id}`, { method: "DELETE" });
}
export const fetchConversations = () => authenticatedJson<Conversation[]>("/api/conversations");
export function createConversation(title = "New conversation") {
  return authenticatedJson<Conversation>("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}
export function renameConversation(id: string, title: string) {
  return authenticatedJson<Conversation>(`/api/conversations/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}
export function removeConversation(id: string) {
  return authenticatedJson<{ status: string }>(`/api/conversations/${id}`, {
    method: "DELETE",
  });
}
export const fetchMessages = (id: string) =>
  authenticatedJson<ChatMessage[]>(`/api/conversations/${id}/messages`);
export function resolveAction(conversationId: string, actionId: string, decision: "confirm" | "dismiss") {
  return authenticatedJson<ActionResult>(`/api/conversations/${conversationId}/actions/${actionId}/${decision}`, {
    method: "POST",
  });
}
/** Choosing a time goes straight to the server's normal checks, without using the LLM. */
export function chooseSlot(conversationId: string, card: SlotsCard, slot: SlotOption) {
  return authenticatedJson<{ question: ChatMessage; message: ChatMessage }>(`/api/conversations/${conversationId}/slot-choice`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      service_id: card.service_id,
      practitioner_id: slot.practitioner_id,
      starts_at: slot.starts_at,
      booking_id: card.booking_id,
    }),
  });
}

/** A guess at the customer's next message; missing it is never worth showing an error for. */
export async function fetchSuggestion(conversationId: string): Promise<string | null> {
  const response = await apiFetch(`/api/conversations/${conversationId}/suggestion`, { method: "POST" }).catch(() => null);
  if (!response?.ok) return null;
  const body = await response.json().catch(() => ({}));
  return typeof body.suggestion === "string" ? body.suggestion : null;
}

/** Sends a message and reports the agent's activity and text as they arrive. */
export async function streamChatMessage(
  id: string,
  content: string,
  signal: AbortSignal,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const response = await apiFetch(`/api/conversations/${id}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
    signal,
  });
  if (!response) throw new Error("You are signed out");
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || "Request failed");
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    buffer += value;
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      if (frame.startsWith("data: ")) onEvent(JSON.parse(frame.slice("data: ".length)));
    }
  }
}
export const fetchAdminOrders = () => authenticatedJson<AdminOrder[]>("/api/admin/orders");
export function advanceOrderStatus(id: string, status: string) {
  return authenticatedJson<{ id: string; status: string }>(`/api/admin/orders/${id}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}
