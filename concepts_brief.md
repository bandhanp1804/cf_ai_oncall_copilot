# On-call Incident Copilot (cf_ai_oncall_copilot)

* **Author:** bandhanp1804
* **Date:** 2026-11-12

## Overview

An on-call incident copilot that runs entirely on Cloudflare. You chat with it
about a failing service ("checkout is throwing 500s"). It investigates with
read-only tools, remembers past incidents, proposes a fix, and runs that fix
only after a person approves it. It ships with a simulated "checkout API" that
emits metrics, logs and deploy events, with faults you can trigger: bad
deploy, database connection exhaustion, and latency spike. Reviewers can run
the whole demo with no real infrastructure.

It is a smaller version of the multi-agent incident agent the author built at
Geotab, so each design choice can be defended from production experience.

## Analysis

Each required component maps to one Cloudflare primitive with a clear job:

| Requirement | Cloudflare piece | Job in the app |
|---|---|---|
| LLM | Workers AI, `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | Reasons over the incident and calls tools: `get_metrics`, `search_logs`, `get_recent_deploys`, `find_similar_incidents`, `propose_remediation` |
| Workflow / coordination | Durable Object per incident, plus a Workflow for remediation | The object runs the chat and tool loop; the fix runs as Workflow steps (snapshot, roll back, verify, report) with retries and a pause for human approval |
| User input | Chat UI (Next.js) | Chat plus an approve or reject card when a fix is proposed |
| Memory / state | Durable Object SQLite storage | Incident timeline, findings, past resolutions and the audit log |

Guardrails are the feature that sets the project apart:

* Read-only by default. Remediation runs only through the Workflow, and the
  Workflow pauses until a person approves.
* No invented numbers. Before showing the model's summary, code checks that
  every number it quotes appears in a tool result.
* Bad tool calls are handled. Arguments are validated against the tool's
  schema; a bad call gets a clear error back and one retry.
* Audit log. Every tool call, proposal, approval and rejection is stored and
  shown in the UI.

## Goals vs. Non-Goals

* **In-Scope Goals:** the four components above; simulated checkout service
  with three injectable faults; similar-incident recall; approval-gated
  remediation Workflow; number check; audit log; README, PROMPTS.md, demo
  video, live URL.
* **Out-of-Scope Non-Goals:** real telemetry sources; authentication on the
  approval step; voice input (stretch); Vectorize (stretch).

## Alternatives Weighed

* **TypeScript Agents SDK (`AIChatAgent`) with WebSocket streaming.** Rejected
  by author: the author asked for a Python backend. The SDK is TypeScript-only,
  so the per-incident agent is a plain Python Durable Object with SQLite.
* **Cloudflare Pages for the UI.** Rejected by author: a Next.js static export
  served from the same Worker's static assets gives one deploy, one URL and no
  CORS. Cloudflare now recommends Workers static assets for new projects.

## Key Decisions & Staging for Next Steps

* Backend in Python Workers; UI in Next.js (stack change requested on
  2026-11-12).
* Build order follows the plan's four evenings: chat, tools and simulator,
  memory and Workflow, guardrails and docs.
* Next step: `/egm-draft` to write `docs/designs/oncall_copilot.md`.
