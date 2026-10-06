"use client";

import { useState } from "react";
import { authConfigured, useSession } from "@/components/Session";

/** Sign-in, and the identity badge in the header. */
export function SignIn() {
  const { me, loading, signIn, signOut } = useSession();
  const [open, setOpen] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (loading) return <span className="meta">…</span>;

  // A named person is signed in.
  if (me && me.role !== "service") {
    return (
      <span className="row" style={{ gap: 8 }}>
        <span className="pill">{me.role}</span>
        <span className="meta">{me.actor}</span>
        <button style={{ padding: "4px 10px", fontSize: 12.5 }} onClick={signOut}>
          Sign out
        </button>
      </span>
    );
  }

  if (!authConfigured) {
    return <span className="pill warn">auth not configured</span>;
  }

  if (!open) {
    return (
      <span className="row" style={{ gap: 8 }}>
        {/* Says plainly what the current identity can and cannot do, rather
            than letting someone discover it by being refused. */}
        <span className="pill">service · read &amp; propose only</span>
        <button style={{ padding: "4px 10px", fontSize: 12.5 }}
                onClick={() => setOpen(true)}>
          Sign in
        </button>
      </span>
    );
  }

  return (
    <form
      className="row"
      style={{ gap: 6 }}
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await signIn(email, password);
          setOpen(false);
        } catch (err) {
          setError(err instanceof Error ? err.message : String(err));
        } finally {
          setBusy(false);
        }
      }}
    >
      <input type="email" required placeholder="email" value={email}
             onChange={(e) => setEmail(e.target.value)}
             style={{ width: 180, padding: "5px 8px", fontSize: 13 }} />
      <input type="password" required placeholder="password" value={password}
             onChange={(e) => setPassword(e.target.value)}
             style={{ width: 130, padding: "5px 8px", fontSize: 13 }} />
      <button className="primary" disabled={busy}
              style={{ padding: "5px 12px", fontSize: 13 }}>
        {busy ? "…" : "Go"}
      </button>
      <button type="button" style={{ padding: "5px 10px", fontSize: 13 }}
              onClick={() => setOpen(false)}>
        Cancel
      </button>
      {error && <span className="pill bad">{error}</span>}
    </form>
  );
}
