"use client";

import { ToolCall } from "@/lib/api";

/**
 * One tool call, collapsed to a line and expandable to everything.
 *
 * The SQL is shown here and nowhere near the model. That asymmetry is the point
 * of the whole design: a person auditing a number needs to see the statement
 * that produced it, and the model must never see the schema it is not allowed
 * to query. The id is the anchor a citation in the findings jumps to.
 */
export function ToolCallCard({ call }: { call: ToolCall }) {
  const repeat = call.result?.repeat_of;
  return (
    <details className={`tool ${call.ok ? "" : "bad"}`} id={`call-${call.call_id}`}>
      <summary>
        <span className={`pill ${call.ok ? "ok" : "bad"}`}>{call.ok ? "ok" : "error"}</span>
        <span className="tool-name">{call.tool}</span>
        <span className="tool-sum">
          {call.ok ? call.result?.summary ?? "(no summary)" : call.error}
        </span>
        {call.duration_ms != null && call.duration_ms > 0 && (
          <span className="meta">{call.duration_ms}ms</span>
        )}
      </summary>
      <div className="tool-body">
        {repeat && (
          <p className="meta" style={{ marginTop: 10 }}>
            Repeat of <code className="mono">{repeat}</code> — not re-executed.
          </p>
        )}

        <div className="label">Arguments</div>
        <pre>{JSON.stringify(call.arguments, null, 2)}</pre>

        {call.result?.used && Object.keys(call.result.used).length > 0 && (
          <>
            <div className="label">Resolved parameters</div>
            <pre>{JSON.stringify(call.result.used, null, 2)}</pre>
          </>
        )}

        {call.ok && call.result?.data != null && (
          <>
            <div className="label">Result</div>
            <pre>{JSON.stringify(call.result.data, null, 2).slice(0, 4000)}</pre>
          </>
        )}

        {call.result?.internals?.sql && (
          <>
            <div className="label">SQL that produced this (not shown to the model)</div>
            <pre>{call.result.internals.sql}</pre>
          </>
        )}
      </div>
    </details>
  );
}
