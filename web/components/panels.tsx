"use client";

import { useState } from "react";
import type { AuditEntry, IncidentSummary, Message, Proposal, Service } from "@/lib/api";
import { TERMINAL } from "@/lib/api";

const FAULTS = [
  { id: "bad_deploy", label: "Bad deploy" },
  { id: "db_pool_exhaustion", label: "DB pool exhaustion" },
  { id: "latency_spike", label: "Latency spike" },
];

function tone(level: "ok" | "warn" | "bad") {
  return {
    ok: "text-emerald-300 border-emerald-500/30",
    warn: "text-amber-300 border-amber-500/40",
    bad: "text-rose-300 border-rose-500/50",
  }[level];
}

function Tile({ label, value, unit, level }: { label: string; value: string; unit: string; level: "ok" | "warn" | "bad" }) {
  return (
    <div className={`rounded-lg border bg-zinc-900/60 px-3 py-2 ${tone(level)}`}>
      <div className="text-[11px] uppercase tracking-wide text-zinc-400">{label}</div>
      <div className="font-mono text-lg tabular-nums">
        {value}
        <span className="ml-1 text-xs text-zinc-500">{unit}</span>
      </div>
    </div>
  );
}

export function ServicePanel({
  service,
  onFault,
  onReset,
  pending,
}: {
  service: Service | null;
  onFault: (fault: string) => void;
  onReset: () => void;
  pending: boolean;
}) {
  const m = service?.metrics;
  const pool = service?.db_pool_size ?? 50;
  return (
    <section className="space-y-3">
      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold text-zinc-200">checkout service</h2>
        <span className="font-mono text-xs text-zinc-400">{service?.version ?? "…"}</span>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Tile label="Error rate" value={m ? m.error_rate_pct.toFixed(1) : "…"} unit="%"
          level={!m ? "ok" : m.error_rate_pct > 5 ? "bad" : m.error_rate_pct > 1 ? "warn" : "ok"} />
        <Tile label="p95 latency" value={m ? String(m.p95_latency_ms) : "…"} unit="ms"
          level={!m ? "ok" : m.p95_latency_ms > 1000 ? "bad" : m.p95_latency_ms > 500 ? "warn" : "ok"} />
        <Tile label="Throughput" value={m ? String(m.rps) : "…"} unit="rps" level="ok" />
        <Tile label="DB pool" value={m ? `${m.db_pool_in_use}/${pool}` : "…"} unit=""
          level={!m ? "ok" : m.db_pool_in_use >= 0.95 * pool ? "bad" : "ok"} />
      </div>
      <div className="text-xs text-zinc-400">
        Active fault:{" "}
        <span className={service?.fault && service.fault !== "none" ? "font-mono text-rose-300" : "font-mono text-emerald-300"}>
          {service?.fault ?? "…"}
        </span>
        <span className="ml-2 text-zinc-500">· {service?.memory_count ?? 0} past incidents in memory</span>
      </div>
      <div>
        <div className="mb-1.5 text-[11px] uppercase tracking-wide text-zinc-500">Trigger a fault</div>
        <div className="flex flex-wrap gap-1.5">
          {FAULTS.map((f) => (
            <button key={f.id} disabled={pending} onClick={() => onFault(f.id)}
              className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2.5 py-1 text-xs text-rose-200 hover:bg-rose-500/20 disabled:opacity-50">
              {f.label}
            </button>
          ))}
          <button disabled={pending} onClick={onReset}
            className="rounded-md border border-zinc-600 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50">
            Reset
          </button>
        </div>
      </div>
    </section>
  );
}

export function IncidentList({
  incidents,
  selected,
  onSelect,
  onNew,
}: {
  incidents: IncidentSummary[];
  selected: string;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const dot: Record<string, string> = { open: "bg-rose-400", mitigating: "bg-amber-400", resolved: "bg-emerald-400" };
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-zinc-200">Incidents</h2>
        <button onClick={onNew} className="rounded-md bg-zinc-100 px-2.5 py-1 text-xs font-medium text-zinc-900 hover:bg-white">
          New incident
        </button>
      </div>
      <ul className="space-y-1">
        {incidents.length === 0 && <li className="text-xs text-zinc-500">No incidents yet.</li>}
        {incidents.map((i) => (
          <li key={i.id}>
            <button onClick={() => onSelect(i.id)}
              className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs ${
                i.id === selected ? "bg-zinc-800 text-zinc-100" : "text-zinc-400 hover:bg-zinc-900"}`}>
              <span className={`h-2 w-2 shrink-0 rounded-full ${dot[i.status] ?? "bg-zinc-500"}`} />
              <span className="truncate">{i.title}</span>
              <span className="ml-auto shrink-0 font-mono text-[10px] text-zinc-500">{i.status}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function NumberBadge({ check }: { check?: Message["meta"]["number_check"] }) {
  if (!check) return null;
  if (check.ok)
    return (
      <span className="rounded border border-emerald-500/30 px-1.5 py-0.5 text-[10px] text-emerald-300"
        title="Every number in this answer appears in a tool result">
        ✓ numbers verified{check.rewritten ? " (after rewrite)" : ""}
      </span>
    );
  return (
    <span className="rounded border border-amber-500/40 px-1.5 py-0.5 text-[10px] text-amber-300"
      title="These numbers were not found in any tool result and were removed">
      ⚠ removed unverified: {check.unverified.join(", ")}
    </span>
  );
}

const STEPS = [
  { key: "snapshot", label: "Snapshot", done: (p: Proposal) => p.steps.includes("report_awaiting") },
  { key: "approval", label: "Approval", done: (p: Proposal) => !["creating", "awaiting_approval"].includes(p.status) },
  { key: "apply", label: "Apply", done: (p: Proposal) => p.steps.includes("report_verifying") },
  { key: "verify", label: "Verify", done: (p: Proposal) => ["succeeded", "failed"].includes(p.status) },
  { key: "report", label: "Report", done: (p: Proposal) => TERMINAL.has(p.status) },
];

const STATUS_STYLE: Record<string, string> = {
  creating: "text-zinc-300",
  awaiting_approval: "text-amber-300",
  approving: "text-sky-300",
  applying: "text-sky-300",
  verifying: "text-sky-300",
  succeeded: "text-emerald-300",
  failed: "text-rose-300",
  stale: "text-rose-300",
  rejected: "text-zinc-400",
  expired: "text-zinc-400",
};

function describe(p: Proposal) {
  if (p.action === "rollback_deploy") return `Roll back ${p.params.expected_version} → ${p.params.target_version}`;
  if (p.action === "scale_db_pool") return `Scale DB pool to ${p.params.pool_size} connections`;
  return "Fail over to the backup payment provider";
}

export function ProposalCard({
  proposal,
  onDecide,
}: {
  proposal: Proposal;
  onDecide: (approve: boolean) => void;
}) {
  const [clicked, setClicked] = useState(false);
  const waiting = proposal.status === "awaiting_approval";
  const failedAt = proposal.status === "rejected" || proposal.status === "expired" ? 1 : proposal.status === "stale" ? 2 : proposal.status === "failed" ? 3 : -1;
  return (
    <div className="mt-2 rounded-lg border border-sky-500/40 bg-sky-500/5 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="text-[11px] uppercase tracking-wide text-sky-300">Proposed remediation</div>
        <div className={`font-mono text-[11px] ${STATUS_STYLE[proposal.status] ?? ""}`}>{proposal.status}</div>
      </div>
      <div className="mt-1 text-sm font-medium text-zinc-100">{describe(proposal)}</div>
      {proposal.reason && <div className="mt-1 text-xs text-zinc-400">{proposal.reason}</div>}
      <ol className="mt-3 flex flex-wrap items-center gap-1 text-[11px]">
        {STEPS.map((s, i) => {
          const done = s.done(proposal);
          const failed = i === failedAt;
          return (
            <li key={s.key} className="flex items-center gap-1">
              <span className={`rounded px-1.5 py-0.5 ${failed ? "bg-rose-500/20 text-rose-300" : done ? "bg-emerald-500/15 text-emerald-300" : "bg-zinc-800 text-zinc-500"}`}>
                {failed ? "✕" : done ? "✓" : "○"} {s.label}
              </span>
              {i < STEPS.length - 1 && <span className="text-zinc-600">→</span>}
            </li>
          );
        })}
      </ol>
      {(waiting || proposal.status === "creating") && (
        <div className="mt-3 flex gap-2">
          <button disabled={!waiting || clicked} onClick={() => { setClicked(true); onDecide(true); }}
            className="rounded-md bg-emerald-500 px-3 py-1.5 text-xs font-semibold text-emerald-950 hover:bg-emerald-400 disabled:opacity-40">
            Approve
          </button>
          <button disabled={!waiting || clicked} onClick={() => { setClicked(true); onDecide(false); }}
            className="rounded-md border border-zinc-600 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-40">
            Reject
          </button>
          {!waiting && <span className="self-center text-[11px] text-zinc-500">Workflow starting…</span>}
        </div>
      )}
    </div>
  );
}

export function ChatMessage({ message, proposal, onDecide }: {
  message: Message;
  proposal?: Proposal;
  onDecide: (proposalId: string, approve: boolean) => void;
}) {
  if (message.role === "notice")
    return (
      <div className="mx-auto max-w-[90%] rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-center text-xs text-zinc-300">
        {message.content}
      </div>
    );
  const user = message.role === "user";
  return (
    <div className={`flex ${user ? "justify-end" : "justify-start"}`}>
      <div className={`max-w-[85%] rounded-xl px-3.5 py-2.5 text-sm leading-relaxed ${user ? "bg-zinc-100 text-zinc-900" : "border border-zinc-800 bg-zinc-900 text-zinc-100"}`}>
        <div className="whitespace-pre-wrap">{message.content}</div>
        {!user && (
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <NumberBadge check={message.meta.number_check} />
            {!!message.meta.tool_calls && <span className="text-[10px] text-zinc-500">{message.meta.tool_calls} tool calls</span>}
          </div>
        )}
        {proposal && <ProposalCard proposal={proposal} onDecide={(a) => onDecide(proposal.id, a)} />}
      </div>
    </div>
  );
}

const KIND_STYLE: Record<string, string> = {
  tool_call: "text-sky-300",
  tool_error: "text-rose-300",
  model_error: "text-rose-300",
  number_check: "text-violet-300",
  proposal: "text-amber-300",
  approval: "text-emerald-300",
  rejection: "text-zinc-300",
  workflow_step: "text-teal-300",
  workflow_error: "text-rose-300",
  incident_opened: "text-zinc-400",
};

function summary(e: AuditEntry) {
  const d = e.detail as Record<string, unknown>;
  if (e.kind === "tool_call" || e.kind === "tool_error") {
    const args = d.args ? JSON.stringify(d.args) : "";
    return `${d.tool}${args ? " " + args : ""}${d.error ? " — " + d.error : ""}`;
  }
  if (e.kind === "number_check") return d.ok ? "passed" : `unverified: ${(d.unverified as string[] | undefined)?.join(", ")}${d.redacted ? " (redacted)" : ""}`;
  if (e.kind === "workflow_step") return `${d.step} → ${d.status}`;
  if (e.kind === "proposal" || e.kind === "approval" || e.kind === "rejection") return `${d.proposal_id} ${d.action ?? ""}`;
  if (e.kind === "model_error") return String(d.error ?? "");
  return d.title ? String(d.title) : "";
}

export function AuditLog({ entries }: { entries: AuditEntry[] }) {
  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold text-zinc-200">Audit log</h2>
      {entries.length === 0 && <p className="text-xs text-zinc-500">Tool calls, proposals and approvals appear here.</p>}
      <ul className="space-y-1">
        {entries.map((e) => (
          <li key={e.id}>
            <details className="group rounded-md border border-zinc-800 bg-zinc-900/50 px-2 py-1.5">
              <summary className="flex cursor-pointer list-none items-baseline gap-2 text-[11px]">
                <span className="font-mono text-zinc-500">{new Date(e.created_at * 1000).toLocaleTimeString()}</span>
                <span className={`font-mono ${KIND_STYLE[e.kind] ?? "text-zinc-300"}`}>{e.kind}</span>
                <span className="truncate text-zinc-400">{summary(e)}</span>
              </summary>
              <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-all font-mono text-[10px] text-zinc-400">
                {JSON.stringify(e.detail, null, 2)}
              </pre>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
}
