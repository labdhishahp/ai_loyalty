"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, ProposalSummary } from "@/lib/api";
import { Empty, Failed, Loading, Status } from "@/components/states";

export default function ProposalsPage() {
  const [proposals, setProposals] = useState<ProposalSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setProposals((await api.listProposals()).proposals);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setProposals([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <>
      <h1>Campaign proposals</h1>
      <p className="lede">
        Drafts the agent produced, and what happened to them. Nothing here has
        been sent unless it says executed — a proposal is inert until a person
        approves it.
      </p>

      {error && <Failed error={error} retry={load} />}
      {!error && proposals === null && <Loading what="proposals" />}
      {!error && proposals?.length === 0 && (
        <Empty
          title="No proposals yet"
          hint="Run an investigation with 'Allow campaign proposals' enabled."
        />
      )}
      {!error && proposals && proposals.length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th style={{ paddingTop: 12 }}>Campaign</th>
                <th>Offer</th>
                <th>Audience</th>
                <th>Checks</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {proposals.map((p) => (
                <tr key={p.proposal_id}>
                  <td>
                    <Link href={`/proposals/${p.proposal_id}`}>{p.name}</Link>
                    <div className="meta">{p.programme} · {p.channel}</div>
                  </td>
                  <td className="mono">
                    {p.offer_type === "points_multiplier"
                      ? `${p.offer_value}x points`
                      : `${p.offer_value}% off`}
                  </td>
                  <td className="mono">{p.audience_size?.toLocaleString() ?? "—"}</td>
                  <td>
                    {p.valid === null ? (
                      <span className="meta">—</span>
                    ) : (
                      <span className={`pill ${p.valid ? "ok" : "bad"}`}>
                        {p.valid ? "pass" : "fail"}
                      </span>
                    )}
                  </td>
                  <td><Status value={p.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
