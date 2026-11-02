"use client";

import { useEffect, useState } from "react";
import {
  Booking,
  BookingQuote,
  cancelBooking,
  confirmBooking,
  fetchAvailability,
  fetchBookings,
  fetchPractitioners,
  fetchServices,
  Practitioner,
  quoteBooking,
  rescheduleBooking,
  Service,
  Slot,
} from "@/lib/api";

function dateValue(day: Date) { return day.toISOString().slice(0, 10); }

export function StudioWorkspace() {
  const [services, setServices] = useState<Service[]>([]);
  const [practitioners, setPractitioners] = useState<Practitioner[]>([]);
  const [bookings, setBookings] = useState<Booking[]>([]);
  const [serviceId, setServiceId] = useState("");
  const [practitionerId, setPractitionerId] = useState("");
  const [slots, setSlots] = useState<Slot[]>([]);
  const [quote, setQuote] = useState<BookingQuote | null>(null);
  const [rescheduleId, setRescheduleId] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function refresh() { const [serviceRows, bookingRows] = await Promise.all([fetchServices(), fetchBookings()]); setServices(serviceRows); setBookings(bookingRows); }
  useEffect(() => { refresh().catch((error) => setMessage(error.message)); }, []);
  async function chooseService(id: string) { setServiceId(id); setPractitionerId(""); setSlots([]); setQuote(null); setPractitioners(id ? await fetchPractitioners(id) : []); }
  async function findSlots() {
    if (!serviceId) return;
    setBusy(true); setMessage("");
    try { const from = new Date(); const to = new Date(); to.setDate(to.getDate() + 14); setSlots(await fetchAvailability(serviceId, dateValue(from), dateValue(to), practitionerId || undefined)); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not find slots"); }
    finally { setBusy(false); }
  }
  async function chooseSlot(slot: Slot) {
    setBusy(true); setMessage("");
    try { setQuote(await quoteBooking(serviceId, slot.practitioner_id, slot.starts_at)); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Slot is unavailable"); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (!quote) return; setBusy(true); setMessage("");
    try { if (rescheduleId) await rescheduleBooking(rescheduleId, quote.starts_at); else await confirmBooking(quote); setMessage(rescheduleId ? "Booking rescheduled." : "Booking confirmed."); setQuote(null); setSlots([]); setRescheduleId(null); await refresh(); }
    catch (error) { setMessage(error instanceof Error ? error.message : "Could not save booking"); }
    finally { setBusy(false); }
  }
  async function cancel(id: string) { setBusy(true); try { await cancelBooking(id); setMessage("Booking cancelled."); await refresh(); } catch (error) { setMessage(error instanceof Error ? error.message : "Could not cancel booking"); } finally { setBusy(false); } }
  async function beginReschedule(booking: Booking) { setRescheduleId(booking.id); await chooseService(booking.service_id); setPractitionerId(booking.practitioner_id); setMessage("Choose a new time for this booking."); }

  return <div className="workspace-grid"><section><p className="eyebrow">Book a visit</p><h2>Find an appointment</h2><div className="booking-filters"><label>Service<select value={serviceId} onChange={(event) => chooseService(event.target.value)}><option value="">Choose a service</option>{services.map((service) => <option key={service.id} value={service.id}>{service.name} · Rs {service.price}</option>)}</select></label><label>Practitioner<select value={practitionerId} onChange={(event) => setPractitionerId(event.target.value)}><option value="">Any practitioner</option>{practitioners.map((practitioner) => <option key={practitioner.id} value={practitioner.id}>{practitioner.name}</option>)}</select></label><button className="button primary" disabled={!serviceId || busy} onClick={findSlots}>Find times</button></div><div className="slot-grid">{slots.map((slot) => <button key={`${slot.practitioner_id}-${slot.starts_at}`} onClick={() => chooseSlot(slot)}><strong>{new Date(slot.local_start).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</strong><span>{slot.practitioner}</span></button>)}</div></section><aside className="workspace-aside">{message && <p className="notice">{message}</p>}{quote && <section className="summary-card"><p className="eyebrow">Confirm before saving</p><h2>{rescheduleId ? "New appointment" : "Booking summary"}</h2><div className="summary-line"><span>Service</span><strong>{quote.service}</strong></div><div className="summary-line"><span>Practitioner</span><strong>{quote.practitioner}</strong></div><div className="summary-line"><span>Time</span><strong>{new Date(quote.starts_at).toLocaleString()}</strong></div><div className="summary-total"><span>Price</span><strong>Rs {quote.price}</strong></div><button className="button primary" disabled={busy} onClick={confirm}>{rescheduleId ? "Confirm reschedule" : "Confirm booking"}</button></section>}<section><p className="eyebrow">Your schedule</p><h2>My bookings</h2><div className="order-list">{bookings.map((booking) => <article className="order-card" key={booking.id}><div><strong>{booking.service}</strong><p>{new Date(booking.local_start).toLocaleString()} · {booking.status}</p></div>{booking.status === "confirmed" && <div className="order-actions"><button onClick={() => beginReschedule(booking)}>Reschedule</button><button onClick={() => cancel(booking.id)}>Cancel</button></div>}</article>)}</div></section></aside></div>;
}
