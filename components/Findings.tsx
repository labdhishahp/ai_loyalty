"use client";

import { Findings as FindingsType } from "@/lib/api";

/**
 * The structured answer.
 *
 * It is structured rather than prose so that most of the eval can be marked
 * deterministically -- did it name a cause, cite evidence, rule the red herring
 * out with a reason, say which comparison it used. The same structure is what
 * makes this screen readable: a reviewer can see the shape of the argument
 * before reading a word of it.
 */
export function Findings({
  findings,
  onCite,
}: {
  findings: FindingsType;
  onCite: (callId: string) => void;
}) {
  return (
    <div className="card">
      <div className="spread">
        <h2 style={{ margin: 0 }}>Findings</h2>
        <span className="pill">{findings.verdict.replace("_", " ")}</span>
      </div>
      <p style={{ fontSize: 16, lineHeight: 1.6 }}>{findings.headline}</p>

      <div className="facts" style={{ margin: "18px 0" }}>
        <div className="fact">
          <div className="k">Measured with</div>
          <div className="v" style={{ fontSize: 13.5 }}>
            {findings.metrics_used.join(", ") || "—"}
          </div>
        </div>
        <div className="fact">
          <div className="k">Compared</div>
          <div className="v" style={{ fontSize: 13.5 }}>{findings.comparison_basis}</div>
        </div>
        <div className="fact">
          <div className="k">Cohort</div>
          <div className="v" style={{ fontSize: 13.5 }}>{findings.cohort_basis}</div>
        </div>
      </div>

      <h3>Causes</h3>
      {findings.causes.length === 0 && <p className="meta">None identified.</p>}
      {findings.causes.map((cause) => (
        <div className={`cause ${cause.importance}`} key={cause.name}>
          <div className="row" style={{ gap: 8, marginBottom: 3 }}>
            <strong>{cause.name}</strong>
            <span className="pill">{cause.importance}</span>
            <span className="meta">{cause.confidence} confidence</span>
          </div>
          <div>{cause.explanation}</div>
          <div className="evidence">
            {cause.evidence_call_ids.map((id) => (
              <button key={id} className="cite" onClick={() => onCite(id)}>
                {id}
              </button>
            ))}
          </div>
        </div>
      ))}

      <h3 style={{ marginTop: 20 }}>Considered and ruled out</h3>
      {findings.ruled_out.length === 0 && (
        <p className="meta">Nothing was explicitly ruled out.</p>
      )}
      {findings.ruled_out.map((item) => (
        <div key={item.name} style={{ marginBottom: 11 }}>
          <strong style={{ fontWeight: 600 }}>{item.name}</strong>
          <div className="meta" style={{ color: "var(--muted)", fontSize: 14 }}>
            {item.why_not}
          </div>
          <div className="evidence">
            {item.evidence_call_ids.map((id) => (
              <button key={id} className="cite" onClick={() => onCite(id)}>
                {id}
              </button>
            ))}
          </div>
        </div>
      ))}

      <div className="facts" style={{ marginTop: 20 }}>
        <div className="fact">
          <div className="k">Limitations</div>
          <div style={{ fontSize: 14 }}>{findings.limitations}</div>
        </div>
        <div className="fact">
          <div className="k">Recommended next</div>
          <div style={{ fontSize: 14 }}>{findings.recommended_next}</div>
        </div>
      </div>
    </div>
  );
}
