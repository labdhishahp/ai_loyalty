"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, AuditEntry } from "@/lib/api";
import { Empty, Failed, Loading } from "@/components/states";

export default function AuditPage() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setEntries((await api.audit()).entries);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setEntries([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <h1>Audit</h1>
      <p className="lede">
        Every consequential act, append-only. Nothing here is ever edited or
        deleted.
      </p>

      {error && <Failed error={error} retry={load} />}
      {!error && entries === null && <Loading what="the audit log" />}
      {!error && entries?.length === 0 && (
        <Empty title="Nothing recorded yet" hint="Proposals and approvals appear here." />
      )}
      {!error && entries && entries.length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th style={{ paddingTop: 12 }}>When</th>
                <th>Actor</th>
                <th>Action</th>
                <th>Subject</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.audit_id}>
                  <td className="meta">{new Date(entry.occurred_at).toLocaleString()}</td>
                  <td>{entry.actor}</td>
                  <td className="mono">{entry.action}</td>
                  <td className="mono">
                    {entry.subject_type}/{entry.subject_id.slice(0, 8)}
                  </td>
                  <td className="mono" style={{ fontSize: 11.5, color: "var(--muted)" }}>
                    {entry.detail ? JSON.stringify(entry.detail) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
