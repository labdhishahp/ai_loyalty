"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, RunSummary, Health } from "@/lib/api";
import { Empty, Failed, Loading, Status } from "@/components/states";

const EXAMPLES = [
  "Why has engagement among Gold customers in the UK dropped over the last three months?",
  "Is the decline in GB Gold engagement a real behaviour change or a composition effect?",
  "Which product category is driving the fall in GB Gold spend, and what does policy allow us to do about it?",
];

export default function InvestigatePage() {
  const [question, setQuestion] = useState("");
  const [allowWrites, setAllowWrites] = useState(false);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [{ runs }, health] = await Promise.all([api.listRuns(), api.health()]);
      setRuns(runs);
      setHealth(health);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setRuns([]);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      const { run_id } = await api.createRun(question.trim(), allowWrites);
      window.location.href = `/runs/${run_id}`;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setStarting(false);
    }
  }

  const tooShort = question.trim().length < 8;

  return (
    <>
      <h1>Investigate</h1>
      <p className="lede">
        Ask a business question. The agent decides what to measure, reads the
        relevant policies and playbooks, and answers with evidence you can click
        through to.
      </p>

      {health && !health.agent_enabled && (
        <div className="error" style={{ marginBottom: 12 }}>
          <strong>The agent is switched off</strong>
          AGENT_ENABLED is false, so no new run will start.
        </div>
      )}

      <div className="card">
        <textarea
          rows={3}
          value={question}
          placeholder="Why has engagement among Gold customers in the UK dropped over the last three months?"
          onChange={(e) => setQuestion(e.target.value)}
        />
        <div className="row" style={{ marginTop: 12 }}>
          <button
            className="primary"
            onClick={start}
            disabled={starting || tooShort || (health ? !health.agent_enabled : false)}
          >
            {starting ? <><span className="spinner" /> Starting…</> : "Investigate"}
          </button>
          <label className="row" style={{ gap: 6, fontSize: 13.5, color: "var(--muted)" }}>
            <input
              type="checkbox"
              checked={allowWrites}
              onChange={(e) => setAllowWrites(e.target.checked)}
            />
            Allow campaign proposals
          </label>
          {health && (
            <span className="meta" style={{ marginLeft: "auto" }}>
              {health.llm_provider_configured}
              {!health.coe_gateway_configured &&
                health.llm_provider_configured === "coe" && " → anthropic (fallback)"}
            </span>
          )}
        </div>
        {!allowWrites && (
          <p className="meta" style={{ marginTop: 8, marginBottom: 0 }}>
            Read-only. The agent is not offered any tool that writes, so it
            cannot create a proposal even if asked to.
          </p>
        )}
      </div>

      <div className="row" style={{ gap: 8, marginBottom: 22 }}>
        {EXAMPLES.map((example) => (
          <button
            key={example}
            style={{ fontSize: 12.5, fontWeight: 450, padding: "6px 10px" }}
            onClick={() => setQuestion(example)}
          >
            {example.length > 52 ? example.slice(0, 52) + "…" : example}
          </button>
        ))}
      </div>

      <h2>Recent investigations</h2>
      {error && <Failed error={error} retry={load} />}
      {!error && runs === null && <Loading what="investigations" />}
      {!error && runs?.length === 0 && (
        <Empty title="No investigations yet" hint="Ask a question above to start one." />
      )}
      {!error && runs && runs.length > 0 && (
        <div className="card" style={{ padding: 0 }}>
          <table>
            <thead>
              <tr>
                <th style={{ paddingTop: 12 }}>Question</th>
                <th>Status</th>
                <th>Steps</th>
                <th>Cost</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id}>
                  <td>
                    <Link href={`/runs/${run.run_id}`}>{run.question}</Link>
                    <div className="meta">{run.provider}/{run.model}</div>
                  </td>
                  <td><Status value={run.status} /></td>
                  <td className="mono">{run.steps_used}</td>
                  <td className="mono">${run.cost_usd.toFixed(3)}</td>
                  <td className="meta">
                    {new Date(run.created_at).toLocaleString()}
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
