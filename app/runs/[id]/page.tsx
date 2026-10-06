"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, Run, TERMINAL, ToolCall } from "@/lib/api";
import { Failed, Loading, Status } from "@/components/states";
import { Findings } from "@/components/Findings";
import { ToolCallCard } from "@/components/ToolCallCard";

/**
 * Drives one run to completion and shows the whole trace.
 *
 * The API executes ONE agent turn per request, so this page is the loop: it
 * calls advance repeatedly until the run reaches a terminal state, rendering
 * what came back each time. That is why the investigation appears step by step
 * rather than after a long silence -- and why closing the tab does not lose it,
 * because every turn is already persisted.
 */
export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [advancing, setAdvancing] = useState(false);
  // Guards against React Strict Mode mounting effects twice in development,
  // which would otherwise run two advance loops against the same run.
  const driving = useRef(false);

  const drive = useCallback(async () => {
    if (driving.current) return;
    driving.current = true;
    try {
      let current = await api.getRun(id);
      setRun(current);
      while (!TERMINAL.includes(current.status)) {
        setAdvancing(true);
        current = await api.advanceRun(id);
        setRun(current);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setAdvancing(false);
      driving.current = false;
    }
  }, [id]);

  useEffect(() => { drive(); }, [drive]);

  async function cancel() {
    try { setRun(await api.cancelRun(id)); } catch { /* the poll will catch up */ }
  }

  function jumpToEvidence(callId: string) {
    const element = document.getElementById(`call-${callId}`);
    if (!element) return;
    (element as HTMLDetailsElement).open = true;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  if (error && !run) return <Failed error={error} retry={drive} />;
  if (!run) return <div style={{ marginTop: 28 }}><Loading what="the investigation" /></div>;

  const callsByStep = new Map<number, ToolCall[]>();
  for (const call of run.tool_calls) {
    callsByStep.set(call.step_no, [...(callsByStep.get(call.step_no) ?? []), call]);
  }
  const progress = Math.min(100, (run.steps_used / run.max_steps) * 100);

  return (
    <>
      <p className="meta" style={{ marginTop: 22 }}>
        <Link href="/">← All investigations</Link>
      </p>
      <h1 style={{ marginTop: 6 }}>{run.question}</h1>

      <div className="row" style={{ marginBottom: 14 }}>
        <Status value={run.status} />
        {advancing && <span className="pill busy"><span className="spinner" /> thinking</span>}
        {run.allow_writes && <span className="pill warn">may propose campaigns</span>}
        <span className="meta">
          {run.provider}/{run.model} · {run.steps_used}/{run.max_steps} steps ·{" "}
          {(run.input_tokens + run.output_tokens).toLocaleString()} tokens ·{" "}
          ${run.cost_usd.toFixed(3)}
        </span>
        {!TERMINAL.includes(run.status) && (
          <button className="danger" style={{ marginLeft: "auto" }} onClick={cancel}>
            Stop
          </button>
        )}
      </div>

      {!TERMINAL.includes(run.status) && (
        <div className="bar" style={{ marginBottom: 20 }}>
          <i style={{ width: `${progress}%` }} />
        </div>
      )}

      {error && <Failed error={error} retry={drive} />}

      {run.status === "budget_exceeded" && (
        <div className="error">
          <strong>Stopped at its budget</strong>
          {run.error} The run is preserved; its findings below are whatever it
          reached before stopping.
        </div>
      )}
      {run.status === "failed" && (
        <div className="error"><strong>The run failed</strong>{run.error}</div>
      )}

      {run.final_answer && (
        <Findings findings={run.final_answer} onCite={jumpToEvidence} />
      )}

      <h2>How it got there</h2>
      {run.steps.length === 0 && !advancing && (
        <div className="empty">No steps recorded.</div>
      )}
      {run.steps.map((step) => (
        <div className="step" key={step.step_no}>
          <div className="step-head">
            <div className="step-no">{step.step_no + 1}</div>
            <span className="meta">
              {step.input_tokens.toLocaleString()} in ·{" "}
              {step.output_tokens.toLocaleString()} out
              {step.duration_ms != null && ` · ${(step.duration_ms / 1000).toFixed(1)}s`}
            </span>
          </div>
          {step.user_message && <div className="nudge">{step.user_message}</div>}
          {step.assistant_text && <p className="thought">{step.assistant_text}</p>}
          {(callsByStep.get(step.step_no) ?? []).map((call) => (
            <ToolCallCard key={call.call_id} call={call} />
          ))}
        </div>
      ))}
    </>
  );
}
