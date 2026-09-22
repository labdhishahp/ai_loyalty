"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";
import { api, ApiError, Proposal } from "@/lib/api";
import { Failed, Loading, Status } from "@/components/states";

/**
 * The approval screen.
 *
 * It is deliberately not a confirmation dialog. Before deciding, a reviewer can
 * see exactly how many people this reaches, who was excluded and why, every
 * governance rule with the policy it comes from, the evidence the agent based
 * it on, and the investigation it came out of. Approving without that is
 * rubber-stamping, and a rubber stamp is not a control.
 *
 * Validation is re-run on load rather than read from the row: people opt out
 * and tiers change, so a stored verdict is a statement about when it was stored.
 */
export default function ProposalPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      // Re-validate first so the screen never shows a stale verdict, then read
      // the full record including approvals and executions.
      await api.revalidate(id).catch(() => undefined);
      setProposal(await api.getProposal(id));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  async function act(what: "approved" | "rejected" | "execute") {
    setBusy(what);
    setActionError(null);
    try {
      if (what === "execute") {
        // Generated once per click and sent with the request, so a retry after
        // a timeout is provably the same request and cannot send twice.
        await api.execute(id, crypto.randomUUID());
      } else {
        await api.decide(id, what, note);
      }
      await load();
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  }

  if (error) return <Failed error={error} retry={load} />;
  if (!proposal) return <div style={{ marginTop: 28 }}><Loading what="the proposal" /></div>;

  const validation = proposal.validation;
  const canDecide = proposal.status === "draft";
  const canExecute = proposal.status === "approved";

  return (
    <>
      <p className="meta" style={{ marginTop: 22 }}>
        <Link href="/proposals">← All proposals</Link>
      </p>
      <h1 style={{ marginTop: 6 }}>{proposal.name}</h1>
      <div className="row" style={{ marginBottom: 16 }}>
        <Status value={proposal.status} />
        {validation && (
          <span className={`pill ${validation.ok ? "ok" : "bad"}`}>
            {validation.ok ? "passes all checks" : "fails validation"}
          </span>
        )}
        {validation?.requires_senior_approval && (
          <span className="pill warn">senior approval required</span>
        )}
        <span className="meta">
          created by {proposal.created_by} ·{" "}
          {new Date(proposal.created_at).toLocaleString()}
        </span>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>What would happen</h2>
        <div className="facts">
          <div className="fact">
            <div className="k">Recipients</div>
            <div className="v">
              {(proposal.audience_size ?? 0).toLocaleString()}
            </div>
          </div>
          <div className="fact">
            <div className="k">Offer</div>
            <div className="v">
              {proposal.offer_type === "points_multiplier"
                ? `${proposal.offer_value}x points`
                : `${proposal.offer_value}% off`}
            </div>
          </div>
          <div className="fact">
            <div className="k">Channel</div>
            <div className="v">{proposal.channel}</div>
          </div>
          <div className="fact">
            <div className="k">Holdout</div>
            <div className="v">{proposal.holdout_pct}%</div>
          </div>
        </div>
        <div className="label">Audience</div>
        <div>{validation?.audience_description ?? "—"}</div>
        <div className="label">Objective</div>
        <div>{proposal.objective}</div>
        <div className="label">Why the agent proposed this</div>
        <div>{proposal.rationale}</div>
        {proposal.run_id && (
          <p className="meta" style={{ marginTop: 12 }}>
            From investigation{" "}
            <Link href={`/runs/${proposal.run_id}`}>{proposal.run_id.slice(0, 8)}</Link>
            {proposal.evidence_call_ids.length > 0 &&
              ` · evidence: ${proposal.evidence_call_ids.join(", ")}`}
          </p>
        )}
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Governance checks</h2>
        {!validation && <p className="meta">Not validated.</p>}
        {validation?.checks.map((check) => (
          <div className={`check ${check.severity}`} key={check.rule + check.detail}>
            <span className="mark">
              {check.severity === "pass" ? "✓" : check.severity === "warn" ? "!" : "✕"}
            </span>
            <div>
              <div>{check.detail}</div>
              <div className="rule">{check.rule} · {check.policy}</div>
            </div>
          </div>
        ))}
      </div>

      {proposal.executions.length > 0 && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Execution</h2>
          {proposal.executions.map((execution) => (
            <div key={execution.execution_id} className="row" style={{ gap: 14 }}>
              <Status value={execution.status} />
              <span className="mono">
                {execution.recipients.toLocaleString()} sent ·{" "}
                {execution.holdout.toLocaleString()} held out
              </span>
              {execution.campaign_id && (
                <span className="meta">campaign #{execution.campaign_id}</span>
              )}
              {execution.error && <span className="pill bad">{execution.error}</span>}
            </div>
          ))}
        </div>
      )}

      {proposal.approvals.length > 0 && (
        <div className="card tight">
          <div className="label" style={{ marginTop: 0 }}>Decisions</div>
          {proposal.approvals.map((a) => (
            <div key={a.approval_id} className="row" style={{ gap: 10, marginTop: 6 }}>
              <Status value={a.decision} />
              <span>{a.actor}</span>
              <span className="meta">{new Date(a.created_at).toLocaleString()}</span>
              {a.note && <span className="meta">“{a.note}”</span>}
            </div>
          ))}
        </div>
      )}

      {actionError && <Failed error={actionError} />}

      {(canDecide || canExecute) && (
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Decision</h2>
          {canDecide && (
            <>
              <input
                type="text"
                placeholder="Optional note for the record"
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              <div className="row" style={{ marginTop: 12 }}>
                <button
                  className="ok"
                  disabled={busy !== null || !validation?.ok}
                  onClick={() => act("approved")}
                >
                  {busy === "approved" ? <><span className="spinner" /> Approving…</> : "Approve"}
                </button>
                <button
                  className="danger"
                  disabled={busy !== null}
                  onClick={() => act("rejected")}
                >
                  Reject
                </button>
                {!validation?.ok && (
                  <span className="meta">
                    A proposal that fails its checks cannot be approved.
                  </span>
                )}
              </div>
            </>
          )}
          {canExecute && (
            <div className="row">
              <button
                className="primary"
                disabled={busy !== null}
                onClick={() => act("execute")}
              >
                {busy === "execute" ? <><span className="spinner" /> Executing…</> : "Execute campaign"}
              </button>
              <span className="meta">
                Validation runs again now. If anything changed since approval,
                execution is refused.
              </span>
            </div>
          )}
        </div>
      )}
    </>
  );
}
