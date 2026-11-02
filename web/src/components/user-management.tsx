"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  createUser,
  deleteUser,
  fetchUsers,
  type Membership,
  updateUserRole,
} from "@/lib/api";

export function UserManagement() {
  const [users, setUsers] = useState<Membership[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    fetchUsers().then(setUsers).catch((reason) => setError(reason.message));
  }, []);

  useEffect(load, [load]);

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    const form = event.currentTarget;
    const values = new FormData(form);
    try {
      await createUser(
        String(values.get("email")),
        String(values.get("role")) as Membership["role"],
      );
      form.reset();
      setMessage("Access added. The user can sign up with this email on this organization's portal.");
      load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not add user");
    } finally {
      setBusy(false);
    }
  }

  async function changeRole(user: Membership, role: Membership["role"]) {
    setError("");
    try {
      const updated = await updateUserRole(user.id, role);
      setUsers((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update role");
    }
  }

  async function remove(user: Membership) {
    if (!window.confirm(`Remove access for ${user.email}?`)) return;
    setError("");
    try {
      await deleteUser(user.id);
      setUsers((current) => current.filter((item) => item.id !== user.id));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not remove user");
    }
  }

  return (
    <section className="user-management">
      <div className="section-heading">
        <div><p className="eyebrow">Access control</p><h2>Users</h2></div>
        <p className="membership-help">Add the email first. The user then signs up from this organization&apos;s branded portal.</p>
      </div>
      <form className="invite-form" onSubmit={add}>
        <label>Email<input name="email" type="email" required placeholder="person@example.com" /></label>
        <label>Role<select name="role" defaultValue="customer"><option value="customer">Customer</option><option value="admin">Admin</option></select></label>
        <button className="button primary" disabled={busy}>{busy ? "Adding…" : "Add user"}</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="notice">{message}</p>}
      <div className="member-list">
        {users.map((user) => (
          <article className="member-row" key={user.id}>
            <div><strong>{user.email}</strong><span className={`status ${user.status}`}>{user.status}</span></div>
            <div className="member-actions">
              <select value={user.role} aria-label={`Role for ${user.email}`} onChange={(event) => changeRole(user, event.target.value as Membership["role"])}>
                <option value="customer">Customer</option><option value="admin">Admin</option>
              </select>
              <button type="button" onClick={() => remove(user)}>Remove</button>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
