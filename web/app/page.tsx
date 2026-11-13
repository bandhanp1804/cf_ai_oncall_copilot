"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, TERMINAL, type IncidentState, type IncidentSummary, type Message, type Service } from "@/lib/api";
import { AuditLog, ChatMessage, IncidentList, ServicePanel } from "@/components/panels";

const SUGGESTIONS = ["Why is checkout failing?", "Have we seen this before?", "What changed recently?"];

function readIncidentId() {
  return new URLSearchParams(window.location.search).get("id") ?? "";
}

function usePoll(fn: () => void, ms: number) {
  const saved = useRef(fn);
  useEffect(() => {
    saved.current = fn;
  });
  useEffect(() => {
    saved.current();
    const t = setInterval(() => saved.current(), ms);
    return () => clearInterval(t);
  }, [ms]);
}

export default function Home() {
  const [service, setService] = useState<Service | null>(null);
  const [incidents, setIncidents] = useState<IncidentSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [state, setState] = useState<IncidentState | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  // Optimistic user bubble; `after` is the last message id seen when it was sent.
  const [pendingUser, setPendingUser] = useState<{ content: string; after: number } | null>(null);
  const [controlsBusy, setControlsBusy] = useState(false);
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setSelected(readIncidentId());
  }, []);

  const select = useCallback((id: string) => {
    setSelected(id);
    setState(null);
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("id", id);
    else url.searchParams.delete("id");
    window.history.replaceState(null, "", url);
  }, []);

  const refreshService = useCallback(() => api.service().then(setService).catch((e) => setError(e.message)), []);
  const refreshIncidents = useCallback(() => api.incidents().then((r) => setIncidents(r.incidents)).catch(() => {}), []);
  const refreshState = useCallback(() => {
    if (!selected) return;
    api.incident(selected).then(setState).catch((e) => setError(e.message));
  }, [selected]);

  const active =
    sending || !!state?.busy || !!state?.proposals.some((p) => !TERMINAL.has(p.status));

  usePoll(refreshService, 3000);
  usePoll(refreshIncidents, 5000);
  usePoll(refreshState, active ? 1500 : 5000);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [state?.messages.length, pendingUser]);

  async function control(fn: () => Promise<Service>) {
    setControlsBusy(true);
    try {
      setService(await fn());
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setControlsBusy(false);
    }
  }

  async function newIncident() {
    const inc = await api.createIncident();
    await refreshIncidents();
    select(inc.id);
    return inc.id;
  }

  async function send(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    setError("");
    setSending(true);
    setPendingUser({ content: message, after: Math.max(0, ...(state?.messages ?? []).map((m) => m.id)) });
    setInput("");
    try {
      const id = selected || (await newIncident());
      await api.chat(id, message);
      setState(await api.incident(id));
      refreshIncidents();
    } catch (e) {
      setError((e as Error).message);
      setInput(message);
    } finally {
      setPendingUser(null);
      setSending(false);
    }
  }

  async function decide(proposalId: string, approve: boolean) {
    try {
      await api.decide(selected, proposalId, approve);
    } catch (e) {
      setError((e as Error).message);
    }
    refreshState();
  }

  const proposals = new Map(state?.proposals.map((p) => [p.id, p]) ?? []);
  const messages: Message[] = state?.messages ?? [];
  // The agent stores the user message before it runs the model, so a poll mid-turn
  // already returns it; drop the optimistic copy once that happens.
  const showPending = !!pendingUser && !messages.some((m) => m.role === "user" && m.id > pendingUser.after);
  // "scripted-fallback" on the latest answer means Workers AI hit its daily quota.
  const lastMode = messages.filter((m) => m.role === "assistant").at(-1)?.meta.llm_mode;
  const fallback = lastMode === "scripted-fallback";
  const mode = fallback ? lastMode : state?.llm_mode;

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 px-4 py-3">
        <div>
          <h1 className="text-base font-semibold">On-call Copilot</h1>
          <p className="text-xs text-zinc-400">
            Investigates with read-only tools · fixes run only after you approve · every number is checked against tool output
          </p>
        </div>
        {mode && (
          <span className={`rounded-full border px-2.5 py-0.5 font-mono text-[11px] ${mode !== "workers-ai" ? "border-amber-500/50 text-amber-300" : "border-zinc-700 text-zinc-400"}`}>
            {mode === "scripted" ? "scripted model" : fallback ? "scripted fallback · AI quota used up" : "Llama 3.3 70B · Workers AI"}
          </span>
        )}
      </header>

      <div className="grid gap-4 p-4 lg:grid-cols-[280px_minmax(0,1fr)_320px]">
        <aside className="space-y-6">
          <ServicePanel service={service} pending={controlsBusy}
            onFault={(f) => control(() => api.setFault(f))} onReset={() => control(api.reset)} />
          <IncidentList incidents={incidents} selected={selected} onSelect={select} onNew={newIncident} />
        </aside>

        <main className="flex min-h-[70vh] flex-col rounded-xl border border-zinc-800 bg-zinc-950">
          <div className="border-b border-zinc-800 px-4 py-2.5 text-sm">
            {state ? (
              <span>
                <span className="font-medium">{state.incident.title}</span>
                <span className="ml-2 font-mono text-xs text-zinc-500">{state.incident.id} · {state.incident.status}</span>
              </span>
            ) : (
              <span className="text-zinc-500">Trigger a fault, then describe what you see. An incident is opened for you.</span>
            )}
          </div>
          <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
            {messages.map((m) => (
              <ChatMessage key={m.id} message={m} onDecide={decide}
                proposal={m.meta.proposal_id ? proposals.get(m.meta.proposal_id) : undefined} />
            ))}
            {showPending && (
              <ChatMessage message={{ id: -1, role: "user", content: pendingUser!.content, meta: {}, created_at: 0 }} onDecide={decide} />
            )}
            {sending && (
              <div className="text-xs text-zinc-400">
                Investigating…{" "}
                {state?.audit.filter((a) => a.kind === "tool_call").slice(0, 1).map((a) => (
                  <span key={a.id} className="font-mono text-sky-300">{String(a.detail.tool)}</span>
                ))}
              </div>
            )}
            <div ref={bottom} />
          </div>
          {fallback && (
            <div className="border-t border-amber-900 bg-amber-950/30 px-4 py-2 text-xs text-amber-200">
              The Workers AI daily free quota is used up, so this answer came from the scripted planner. Llama is back after
              00:00 UTC.
              {state?.fallback_url && (
                <> Always-on scripted demo: <a href={state.fallback_url} className="underline">{new URL(state.fallback_url).host}</a></>
              )}
            </div>
          )}
          {error && <div className="border-t border-rose-900 bg-rose-950/40 px-4 py-2 text-xs text-rose-300">{error}</div>}
          <div className="border-t border-zinc-800 p-3">
            <div className="mb-2 flex flex-wrap gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button key={s} disabled={sending} onClick={() => send(s)}
                  className="rounded-full border border-zinc-700 px-2.5 py-1 text-xs text-zinc-300 hover:bg-zinc-900 disabled:opacity-50">
                  {s}
                </button>
              ))}
            </div>
            <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); send(input); }}>
              <input value={input} onChange={(e) => setInput(e.target.value)} disabled={sending}
                placeholder="checkout is throwing 500s…" maxLength={2000}
                className="flex-1 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm outline-none focus:border-zinc-500" />
              <button type="submit" disabled={sending || !input.trim()}
                className="rounded-lg bg-zinc-100 px-4 py-2 text-sm font-medium text-zinc-900 hover:bg-white disabled:opacity-40">
                Send
              </button>
            </form>
          </div>
        </main>

        <aside>
          <AuditLog entries={state?.audit ?? []} />
        </aside>
      </div>
    </div>
  );
}
