/**
 * Every call to the Python API goes through /api/proxy, never straight to it.
 *
 * WHY. The API is protected by a shared secret. A browser that held that secret
 * would expose it to anyone who opened devtools, and a NEXT_PUBLIC_ variable is
 * compiled into the bundle by definition. The proxy is a server-side route
 * handler: it holds the key, the browser holds nothing.
 *
 * It also means the browser has one origin to talk to, so no CORS in production.
 */

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  // Attach the signed-in user's token when there is one. The proxy forwards it
  // and the API resolves a named person with a role; without it the proxy falls
  // back to the service key, which may read and draft but never approve.
  const { supabase } = await import("./supabase");
  const token = supabase
    ? (await supabase.auth.getSession()).data.session?.access_token
    : undefined;

  const response = await fetch(`/api/proxy${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers ?? {}),
    },
  });
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) {
    // FastAPI puts the message in `detail`; anything else is a surprise and is
    // shown verbatim rather than replaced with something reassuring.
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }
  return body as T;
}

export const api = {
  health: () => call<Health>("/health"),
  me: () => call<Me>("/me"),

  listRuns: (limit = 25) => call<{ runs: RunSummary[] }>(`/runs?limit=${limit}`),
  createRun: (question: string, allowWrites: boolean) =>
    call<{ run_id: string }>("/runs", {
      method: "POST",
      body: JSON.stringify({ question, allow_writes: allowWrites }),
    }),
  getRun: (id: string) => call<Run>(`/runs/${id}`),
  advanceRun: (id: string) => call<Run>(`/runs/${id}/advance`, { method: "POST" }),
  cancelRun: (id: string) => call<Run>(`/runs/${id}/cancel`, { method: "POST" }),

  listProposals: (limit = 25) =>
    call<{ proposals: ProposalSummary[] }>(`/proposals?limit=${limit}`),
  getProposal: (id: string) => call<Proposal>(`/proposals/${id}`),
  revalidate: (id: string) =>
    call<Proposal>(`/proposals/${id}/revalidate`, { method: "POST" }),
  decide: (id: string, decision: "approved" | "rejected", note?: string) =>
    call<{ decision: string }>(`/proposals/${id}/decision`, {
      method: "POST",
      body: JSON.stringify({ decision, note: note || null }),
    }),
  execute: (id: string, idempotencyKey: string) =>
    call<Execution>(`/proposals/${id}/execute`, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    }),

  audit: (limit = 50) => call<{ entries: AuditEntry[] }>(`/audit?limit=${limit}`),
};

export const TERMINAL = ["completed", "failed", "budget_exceeded", "cancelled"];

export type Me = {
  actor: string;
  role: "analyst" | "approver" | "service";
  scopes: string[];
};

export type Health = {
  ok: boolean;
  llm_provider_configured: string;
  openai_compatible_configured: boolean;
  agent_enabled: boolean;
  auth_required: boolean;
};

export type RunSummary = {
  run_id: string;
  question: string;
  status: string;
  provider: string;
  model: string;
  steps_used: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  created_at: string;
  completed_at: string | null;
};

export type ToolCall = {
  call_id: string;
  step_no: number;
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  result: {
    summary?: string;
    data?: unknown;
    used?: Record<string, unknown>;
    repeat_of?: string;
    internals?: { sql?: string; params?: Record<string, string> };
  } | null;
  error: string | null;
  duration_ms: number | null;
};

export type Step = {
  step_no: number;
  user_message: string | null;
  assistant_text: string | null;
  stop_reason: string | null;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number | null;
};

export type Cause = {
  name: string;
  explanation: string;
  evidence_call_ids: string[];
  importance: "largest" | "significant" | "minor";
  confidence: "high" | "medium" | "low";
};

export type Findings = {
  headline: string;
  verdict: string;
  metrics_used: string[];
  comparison_basis: string;
  cohort_basis: string;
  causes: Cause[];
  ruled_out: { name: string; why_not: string; evidence_call_ids: string[] }[];
  limitations: string;
  recommended_next: string;
};

export type Run = RunSummary & {
  allow_writes: boolean;
  actor: string;
  scopes: string[];
  error: string | null;
  final_answer: Findings | null;
  max_steps: number;
  max_cost_usd: number | null;
  steps: Step[];
  tool_calls: ToolCall[];
};

export type Check = {
  rule: string;
  severity: "pass" | "warn" | "fail";
  detail: string;
  policy: string;
};

export type Validation = {
  ok: boolean;
  audience_size: number;
  audience_description: string;
  requires_senior_approval: boolean;
  checks: Check[];
};

export type ProposalSummary = {
  proposal_id: string;
  run_id: string | null;
  status: string;
  name: string;
  programme: string;
  channel: string;
  offer_type: string;
  offer_value: number;
  audience_size: number | null;
  valid: boolean | null;
  created_by: string;
  created_at: string;
};

export type Proposal = ProposalSummary & {
  objective: string;
  rationale: string;
  evidence_call_ids: string[];
  holdout_pct: number;
  audience_spec: Record<string, unknown>;
  validation: Validation | null;
  content_hash: string | null;
  approvals: {
    approval_id: string;
    decision: string;
    actor: string;
    note: string | null;
    audience_size: number;
    created_at: string;
  }[];
  executions: Execution[];
};

export type Execution = {
  execution_id: string;
  status: string;
  campaign_id: number | null;
  recipients: number;
  holdout: number;
  error: string | null;
  started_at?: string;
  finished_at?: string | null;
  replayed?: boolean;
};

export type AuditEntry = {
  audit_id: number;
  occurred_at: string;
  actor: string;
  action: string;
  subject_type: string;
  subject_id: string;
  detail: Record<string, unknown> | null;
};
