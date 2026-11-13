// Thin client for the Worker's /api routes. Every request carries the
// browser's sandbox id so each visitor gets their own simulated service.

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";
const SANDBOX_KEY = "oncall-copilot-sandbox";

export type Metrics = {
  error_rate_pct: number;
  p95_latency_ms: number;
  rps: number;
  db_pool_in_use: number;
};

export type Service = {
  version: string;
  fault: string;
  db_pool_size: number;
  metrics: Metrics;
  deploys: { version: string; at: string; author: string; summary: string }[];
  memory_count: number;
};

export type IncidentSummary = { id: string; title: string; status: string; created_at: number };

export type NumberCheck = { ok: boolean; unverified: string[]; rewritten?: boolean; redacted?: boolean };

export type Message = {
  id: number;
  role: "user" | "assistant" | "notice";
  content: string;
  meta: { number_check?: NumberCheck; proposal_id?: string; tool_calls?: number; llm_mode?: string };
  created_at: number;
};

export type Proposal = {
  id: string;
  action: string;
  params: { target_version: string; pool_size: number; expected_version: string; expected_fault: string };
  reason: string;
  status: string;
  steps: string[];
  created_at: number;
};

export type AuditEntry = { id: number; kind: string; detail: Record<string, unknown>; created_at: number };

export type IncidentState = {
  incident: { id: string; title: string; status: string; created_at: number };
  messages: Message[];
  audit: AuditEntry[];
  proposals: Proposal[];
  busy: boolean;
  llm_mode: string;
  fallback_url?: string;
};

export const TERMINAL = new Set(["rejected", "succeeded", "failed", "stale", "expired"]);

function sandboxId(): string {
  let sid = "";
  try {
    sid = localStorage.getItem(SANDBOX_KEY) ?? "";
  } catch {}
  if (!/^[a-f0-9]{12}$/.test(sid)) {
    const bytes = crypto.getRandomValues(new Uint8Array(6));
    sid = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    try {
      localStorage.setItem(SANDBOX_KEY, sid);
    } catch {}
  }
  return sid;
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json", "x-sandbox-id": sandboxId() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error ?? `HTTP ${res.status}`);
  return data as T;
}

export const api = {
  service: () => call<Service>("/api/service"),
  setFault: (fault: string) => call<Service>("/api/service/fault", { fault }),
  reset: () => call<Service>("/api/service/reset", {}),
  incidents: () => call<{ incidents: IncidentSummary[] }>("/api/incidents"),
  createIncident: (title?: string) => call<IncidentSummary>("/api/incidents", { title }),
  incident: (id: string) => call<IncidentState>(`/api/incidents/${id}`),
  chat: (id: string, message: string) => call<{ message: Message }>(`/api/incidents/${id}/chat`, { message }),
  decide: (id: string, proposalId: string, approve: boolean) =>
    call<{ status: string }>(`/api/incidents/${id}/proposals/${proposalId}/${approve ? "approve" : "reject"}`, {}),
};
