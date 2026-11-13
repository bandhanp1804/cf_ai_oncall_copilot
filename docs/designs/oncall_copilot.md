# On-call Incident Copilot

*   **Author:** bandhanp1804
*   **Status:** Implemented and deployed. Goldfish pass 1 is resolved (see
    `review/goldfish_review_report.md`).
*   **Date:** 2026-11-12
*   **Writing standard:** ASD-STE100 (Simplified Technical English).
    Descriptive sentences have a maximum of 25 words. Instructions have a
    maximum of 20 words. Code names (for example `get_metrics`) are technical
    names. They are not translated.

--------------------------------------------------------------------------------

## Overview

`cf_ai_oncall_copilot` is a chat assistant for an on-call engineer. All of
it runs on Cloudflare.

The engineer opens an incident and writes a message, for example "checkout is
throwing 500s". The copilot uses read-only tools to find the cause. It
compares the incident with past incidents. Then it proposes one fix.

The fix runs only after the engineer approves it. A durable Workflow runs the
fix in four steps: snapshot, apply, verify and report.

The app has a simulated checkout API. Thus, a reviewer can run the full demo
without real infrastructure. The simulator makes metrics, logs and deploy
events. It has three faults. The user starts a fault from the UI.

The stack has two parts:

*   A Python Worker with Durable Objects, a Python Workflow, and Workers AI
    through the `AI` binding.
*   A Next.js UI. The build makes a static export. The same Worker serves the
    export as static assets.

## The Problem

At the start of an incident, an on-call engineer collects the same data each
time. The data is the error rate, the latency, the recent deploys and the
related log lines. Then the engineer finds out if the team saw the incident
before. This work uses the first minutes of the incident.

An LLM can collect this data. But in incident response, two failures are not
permitted:

1.  The model changes production without approval.
2.  The model quotes a number that no system reported.

The copilot collects the data with tools that can only read. The one action
that changes the service is a separate Workflow. The Workflow is durable, and
it waits for a human approval. Before the UI shows an answer, code compares
each number in the answer with the tool results.

## Goals

*   G1. The user starts the `bad_deploy` fault and asks "why is checkout
    failing?". The answer names the bad version (`v2.15.0`). It quotes an
    error rate from a `get_metrics` result.
*   G2. Only `RemediationWorkflow` changes the simulator state. The demo
    controls `set_fault` and `reset` are the exceptions. The UI calls them,
    and the model cannot call them. `apply_remediation` refuses a request
    unless the incident reports the proposal as `approving`. Only a human
    Approve click sets this status.
*   G3. The first `bad_deploy` incident is resolved. Then the answer for a
    second `bad_deploy` incident says that a past incident matches. It says
    that a rollback fixed that incident. This text comes from
    `find_similar_incidents` output.
*   G4. The incident audit log records each tool call, validation error,
    proposal, approval, rejection, Workflow step and number-check result. The
    UI shows the audit log.
*   G5. A malformed tool call never causes an exception in the chat request.
    Examples are an unknown tool, a missing argument, or an argument of the
    wrong type. The model gets one error message for each tool in a turn. A
    second bad call to the same tool in the same turn stops the tool loop. The
    user then gets a message.
*   G6. `python3 -m unittest discover -s tests` passes on CPython 3.13. It
    needs no installed packages.
*   G7. When the service is healthy, the copilot does not propose a fix. This
    is also true when the question says that checkout is failing.
*   G8. When Workers AI has no daily quota, the demo continues to work. The
    scripted planner answers, and the UI tells the user.

## Non-Goals

*   Real telemetry, real deploy systems, or real paging integrations.
*   Authentication or authorization on an endpoint. This includes approval.
*   Streaming of model output token by token (see Alternative B).
*   Voice input through Realtime, and Vectorize. Both are stretch goals.
*   More than one service. The simulator has one service, `checkout`.

## Open Questions

*   **Q1: The Llama 3.3 tool-message format on Workers AI.** The
    documentation does not show how to send tool results back to this model.
    The design sends `{"role": "tool", "name": ..., "content": ...}`. Before
    it, an assistant message repeats the call. The parser also accepts a tool
    call that is JSON text in `response`. **Result:** verified on the first
    deploy (V5). The model uses both forms.
*   **Q2: Workers AI in local dev.** The `AI` binding always calls the remote
    service. Thus, `wrangler dev` needs `wrangler login` and a `workers.dev`
    subdomain. Without them, use the `scripted` environment (see §6).

## Critical User Journeys (CUJs)

### CUJ 1: Diagnose a bad deploy

*   **Given**: the simulator is healthy on `v2.14.1`. There are no stored
    past incidents.
*   **When**: the user clicks "Bad deploy" and sends "checkout is throwing
    500s, why?". The UI opens an incident.
*   **Then**: the audit log shows calls to `get_metrics`,
    `get_recent_deploys` and `search_logs`. The answer names `v2.15.0`. It
    quotes an error rate from a tool result. It has a "numbers verified"
    badge. A proposal card follows: "Roll back to v2.14.1".

### CUJ 2: Approve and remediate

*   **Given**: CUJ 1 is complete, and a proposal waits for approval.
*   **When**: the user clicks Approve.
*   **Then**: the UI shows the Workflow steps in sequence: snapshot,
    approval, apply, verify and report. Verify can retry while the error rate
    decreases. The incident status changes to `resolved`. The chat gets a
    message with the verified error rate. Service memory stores the incident.

### CUJ 3: Reject a proposal

*   **Given**: a proposal waits for approval.
*   **When**: the user clicks Reject.
*   **Then**: the Workflow stops before the apply step. The proposal status
    is `rejected`. The audit log has a `rejection` entry. The fault stays
    active, and the user can continue the chat.

### CUJ 4: Recall a past fix

*   **Given**: CUJ 2 is complete one time, and the service is healthy.
*   **When**: the user starts the bad deploy again, opens a new incident and
    asks for the cause.
*   **Then**: `find_similar_incidents` returns the first incident. Its score
    is 0.5 or more. The expected score is 1.0, because the tags are the same.
    The answer says that a rollback fixed this incident before.

### CUJ 5: A wrong fix does not hide the fault

*   **Given**: the `db_pool_exhaustion` fault is active.
*   **When**: the user approves a `rollback_deploy` proposal. This can occur
    if the model selects the wrong fix.
*   **Then**: apply is successful. Verify uses all its retries and fails. The
    proposal status is `failed`. The incident stays open. The chat tells the
    user that verification failed.

### CUJ 6: The model quotes an invented number

*   **Given**: the draft answer of the model contains "error rate is 40%".
    No tool result contains 40.
*   **When**: the number check runs.
*   **Then**: the model gets one rewrite request with a list of the
    unverified numbers. If the rewrite also fails the check, the code
    replaces those numbers with `[unverified]`. The UI shows a warning badge.
    The audit log gets a `number_check` failure.

### CUJ 7: A healthy service gets no fix

*   **Given**: no fault is active.
*   **When**: the user asks "Why is checkout failing?".
*   **Then**: the answer says that checkout looks healthy. There is no
    proposal. If the model tries to propose a fix, the code blocks it (§5).

### CUJ 8: The Workers AI daily quota is empty

*   **Given**: Workers AI returns error `4006` (daily free allocation used
    up).
*   **When**: the user sends a message.
*   **Then**: the scripted planner completes the turn. The audit log gets a
    `model_fallback` entry. The UI shows an amber badge and a banner. The
    banner has a link to the fallback Worker.

## Technical Plan (Architecture)

```text
 Browser (Next.js static export)
   │  fetch /api/* with X-Sandbox-Id (poll GET /api/incidents/{id} every 1.5 s while busy)
   ▼
 Default (Python WorkerEntrypoint)      ── serves web/out through ASSETS for other paths
   │ RPC (JSON strings)                 │ RPC
   ▼                                    ▼
 IncidentAgent (DO, 1 per incident)   CheckoutService (DO, 1 per sandbox "checkout-<sid>")
   SQLite: incident, messages,          SQLite: state, deploys, memory (past incidents),
           findings, audit_log,                 incident_index
           proposals, workflow_steps     simulator: metrics and logs from (state, now)
   tool loop ──► Workers AI (Llama 3.3)   read-only methods + apply_remediation()
             └─► ScriptedModel (scripted mode, or fallback on error 4006)
   read-only tools ──────────────────────►        ▲
   propose_remediation ──► REMEDIATION.create()   │ only caller of apply_remediation
                                     │            │
                                     ▼            │
                     RemediationWorkflow (Python Workflow)
                       snapshot → wait_for_event("approval") → apply → verify → report
                       report ──► IncidentAgent.workflow_update / CheckoutService.remember
```

### 1. Components

*   **`Default` (Worker entrypoint).** It sends `/api/*` requests to the
    Durable Objects. It sends all other paths to `env.ASSETS`. It keeps no
    state.
*   **`IncidentAgent` (Durable Object, one for each incident id).** It owns
    the conversation, the tool loop, the findings, the proposals and the
    audit log of the incident.
    *   It runs one chat turn at a time. A second `chat` call during a turn
        returns `{"error": "busy"}`, and the Worker returns HTTP 409.
    *   Reads (`get_state`) can occur during a turn. The turn waits for
        outbound I/O, and this opens the input gate of the object.
*   **`CheckoutService` (Durable Object, one for each sandbox).** It owns the
    simulator state and the memory of past incidents.
    *   Its read methods calculate metrics and logs from the stored state and
        the current time.
    *   It has one write method, `apply_remediation`. Only
        `RemediationWorkflow` calls it.
    *   `set_fault` and `reset` are demo controls for the fault buttons. They
        are not tools.
*   **`RemediationWorkflow` (Python Workflow).** There is one instance for
    each proposal. `propose_remediation` creates it.

Memory is in `CheckoutService`, not in `IncidentAgent`. The object of one
incident cannot read the storage of a different incident. The service history
is the correct owner of "how did we fix this before".

### 2. RPC boundary

All Durable Object RPC methods receive and return JSON strings
(`json.dumps` and `json.loads`). The Python Workers SDK converts dicts for
binding calls. But a value in a Durable Object method can be a `JsProxy`
object. A string crosses the FFI without conversion rules.

Workflow `params` and event payloads are dicts. The SDK wraps `create`,
`send_event` and `run(event, step)` with `python_to_rpc` and
`python_from_rpc`.

### 3. Simulator (`src/sim.py`, pure Python)

The state has these fields: `version`, `fault`, `fault_started_at`,
`remediated_at` and `db_pool_size` (50). The `fault` field has one of these
values: `none`, `bad_deploy`, `db_pool_exhaustion` or `latency_spike`.

There are two seed deploys: `v2.14.0` (two days ago) and `v2.14.1` (one day
ago). The `bad_deploy` fault adds `v2.15.0` ("Migrate payment client to
PaymentClientV2") at the current time. It also sets `version`.

The simulator calculates metrics for each minute from `(state, t)`. A
deterministic hash of `(metric, minute)` gives the jitter. Thus, two calls in
the same minute return the same numbers. This keeps the number check stable.

| Fault | error_rate_pct | p95_latency_ms | rps | db_pool_in_use | Log signature | Fix |
|---|---|---|---|---|---|---|
| none | 0.3–0.6 | 180–220 | 420–480 | 18–26 of 50 | INFO only | — |
| bad_deploy | 22–26 | 320–380 | 420–480 | 18–26 | `NullPointerException at PaymentClientV2.authorize` (HTTP 500, version v2.15.0) | `rollback_deploy` to the previous version |
| db_pool_exhaustion | 8–12 | 2600–3000 | 520–600 | 50 of 50 | `Timeout acquiring DB connection after 30000ms (pool 50/50 in use)` | `scale_db_pool` to more than 50 |
| latency_spike | 2–3 | 3900–4300 | 420–480 | 30–38 | `payments-gateway responded in 3900ms (timeout 4000ms)` | `failover_payment_provider` |

A fault gets to its full effect 10 s after `fault_started_at`. This short
time makes the demo fast. After the matching fix, the values decrease
linearly to the baseline in 20 s from `remediated_at`. Then the fault
clears. A fix that does not match the fault sets `remediated_at`. But it does
not decrease the effect of the fault.

The simulator rounds the values when it makes them. Percentages have 1
decimal place. Latency and counts are integers.

### 4. Tools

Each tool schema is flat. It has only top-level scalar properties, a maximum
of four properties, and enums for closed sets. `src/tools.py` defines each
schema one time. The model gets the schemas, and the validator uses them.

| Tool | Arguments | Returns |
|---|---|---|
| `get_metrics` | `metric` enum (`error_rate_pct`, `p95_latency_ms`, `rps`, `db_pool_in_use`), `window_minutes` int 5–60 (default 15) | `{metric, unit, points: [{minute, value}], current, baseline, max, ratio_to_baseline}` |
| `search_logs` | `query` string (optional, substring), `level` enum (`ERROR`, `WARN`, `INFO`, optional), `limit` int 1–50 (default 20) | `{matches, total_matching, lines: [{ts, level, msg}]}` for the last 15 minutes |
| `get_recent_deploys` | `limit` int 1–10 (default 5) | `{current_version, deploys: [{version, at, author, summary}]}` |
| `find_similar_incidents` | `query` string (optional) | `{fingerprint, matches: [{incident_id, title, score, root_cause, remediation, resolved_at}]}` |
| `propose_remediation` | `action` enum (`rollback_deploy`, `scale_db_pool`, `failover_payment_provider`), `target_version` string (for rollback), `pool_size` int 60–200 (for scale), `reason` string | `{proposal_id, status: "pending_approval"}`, or an error |

`propose_remediation` does not change the service state. It does these
steps:

1.  It checks the arguments of the action. A rollback target must be a
    version that exists and is older than the current version.
2.  It stores a proposal.
3.  It creates the Workflow instance.

It refuses a new proposal while a different proposal is open.

**Validation (`src/guardrails.py: validate_args`).** The validator checks
these items:

*   The required properties.
*   The type (`string` or `integer`). It does not accept `bool` as an
    integer.
*   `enum`, `minimum` and `maximum`.

It removes unknown properties and adds the defaults. It converts an integer
sent as a string (`"15"`), because Llama often sends integers in this form.
If validation fails, the tool result is `{"error": "<problem>", "expected":
<schema>}`.

In each turn, a counter records the failures for each tool name. The second
failure for the same tool stops the loop. The code then writes a
`tool_error` audit entry, and the model writes an answer from the evidence.

**Similarity (`src/memory.py`).** A fingerprint is a set of tags. Code
calculates it from the current telemetry, not from model text:

*   `high_error_rate`: `error_rate_pct` is more than 5.
*   `high_latency`: p95 is more than 1000 ms.
*   `pool_saturated`: 95% or more of the pool is in use.
*   `recent_deploy`: the newest deploy is less than 30 minutes old.
*   `sig:<log signature id>`: the most frequent ERROR signature.

The score is the Jaccard index of the current tags and the stored tags. The
score gets 0.1 more if `query` and the stored title share a word of four or
more letters. The maximum score is 1.0, rounded to 2 decimal places. A match
needs a score of 0.5 or more. The tool returns the top 3 matches.

### 5. Chat turn (`src/copilot.py`, pure Python)

`Copilot.run_turn(...)` receives three async callables:
`run_model(messages, tools) -> dict`, `execute_tool(name, args) -> dict` and
`audit(kind, detail)`. Thus, tests can run it on CPython.

1.  Make the messages. They are the system prompt (`src/prompts.py`), the
    last 12 stored messages, and the new user message.
2.  Do the loop, with a maximum of 6 model calls:
    *   Call the model. Get the tool calls from `tool_calls`, or from a JSON
        object in `response`.
    *   If there are no tool calls, the `response` text is the draft answer.
        Stop the loop.
    *   If there are tool calls, validate and run each call. Record the
        findings and an audit entry. Add the assistant tool-call message and
        one `tool` message for each call. Cut each result to 4,000
        characters.
3.  If the loop uses all 6 calls without an answer, ask for an answer one
    time. Send this request without tools.
4.  Do the number check (below). If it fails, ask for one rewrite. Then do
    the check again.
5.  Store the final assistant message with its metadata: the
    `number_check` result, the `proposal_id` from this turn, and `llm_mode`.

**Number check (`src/guardrails.py: check_numbers`).** A regex finds the
numbers in the answer. The check ignores numbers in version strings
(`v2.15.0`) and in incident or proposal ids. It removes `,` and a trailing
`%`.

The allowed set has three parts:

*   Each number in the JSON of the findings of this incident.
*   Each number in the messages of the user.
*   The integers 0 to 10 and 100, for counts and list markers.

A number passes if it is equal to an allowed value. It also passes if an
allowed value, rounded to the same precision, is equal to it. For example,
`24` matches `23.8`, and `23.8` matches `23.84`. The check returns `{ok,
unverified: [..]}`.

**Evidence gate for proposals (`src/guardrails.py: shows_problem`).** The
code accepts `propose_remediation` only if a tool result shows a symptom.
There are two types of symptom:

*   A metric with `ratio_to_baseline` of 1.5 or more.
*   A log line with level ERROR or WARN.

A healthy service has a ratio of 1.0 and only INFO logs. Each fault moves
one or more metrics much higher than 1.5. If there is no symptom, the model
gets a tool error. The error tells the model to check the other metrics and
the logs. If they are also normal, the model must say that checkout looks
healthy. The audit log records the blocked call.

The system prompt also has this rule. But a live test showed that the rule
alone does not stop Llama (see V6). Thus, the gate is in code.

### 6. Model modes

The wrangler variable `LLM_MODE` sets the mode. There are three values for
the `llm_mode` field of an answer:

| `llm_mode` | Model | When |
|---|---|---|
| `workers-ai` | `env.AI.run("@cf/meta/llama-3.3-70b-instruct-fp8-fast", {messages, tools, max_tokens: 1024, temperature: 0.2})` | Default |
| `scripted` | `ScriptedModel` (`src/scripted.py`) | The `scripted` environment (`npm run dev:scripted`, and the fallback Worker) |
| `scripted-fallback` | `ScriptedModel`, after Workers AI | A Workers AI call failed with error `4006` during this turn |

`ScriptedModel` is a fixed planner. It calls `get_metrics`,
`get_recent_deploys`, `search_logs` and `find_similar_incidents`. Then it
proposes the matching fix. It writes its answer only from tool results. It
is the test double for `Copilot`. It also lets `wrangler dev` run the full
demo without a Cloudflare login.

**Quota fallback (`IncidentAgent._turn`).** On the free plan, Workers AI
gives 10,000 neurons each day. When a call fails with error `4006`, these
steps occur:

1.  The agent writes a `model_fallback` audit entry.
2.  `ScriptedModel` answers this call and all the remaining calls of the
    turn.
3.  The answer gets `llm_mode: "scripted-fallback"`.

`ScriptedModel` reads only the messages. Thus, it can continue a turn that
Llama started. Other model errors do not cause the fallback. They give the
`model_error` result (R3).

The UI shows a badge for each mode that is not `workers-ai`. For
`scripted-fallback`, it also shows a banner. The banner tells the user that
Llama returns after 00:00 UTC. It has a link to `FALLBACK_URL` (§11).

### 7. Remediation Workflow (`src/remediation.py`)

The params are `{incident_id, sid, proposal_id, action, target_version,
pool_size, expected_version, expected_fault}`.

1.  `snapshot` (`step.do`): `CheckoutService.snapshot()` returns the
    version, the fault and the current metrics. A second step reports
    `awaiting_approval` to the incident.
2.  `step.wait_for_event("wait for approval", "approval", timeout="1
    hour")`. If the payload has `approved: false`, report `rejected` and
    stop. If the wait times out (exception), report `expired` and stop.
3.  `apply` (`step.do`, 3 retries, 2 s, exponential):
    `CheckoutService.apply_remediation(...)`.
4.  `verify` (`step.do`, 4 retries, 3 s, exponential, 30 s timeout): read
    the metrics. Raise an exception unless `error_rate_pct < 1.0` and
    `p95_latency_ms < 500`. If all retries fail, report `failed` with the
    last snapshot and stop.
5.  `report` (`step.do`): `IncidentAgent.workflow_update(succeeded, …)`. The
    agent sets the incident status to `resolved`. It adds a message with the
    values from the verify snapshot. It calls `CheckoutService.remember` with
    three items: the fingerprint from the proposal time, the root cause from
    the proposal reason, and the action.

Each step reports to `IncidentAgent.workflow_update`. This method writes a
`workflow_step` audit entry and updates the proposal status. The UI shows
these statuses.

The approval path is `POST /api/incidents/{id}/proposals/{pid}/approve` →
`IncidentAgent.decide(pid, approved)`. `decide` does these steps:

1.  It checks that the proposal status is `awaiting_approval`.
2.  It writes an `approval` or `rejection` audit entry.
3.  It sends the event: `(await
    env.REMEDIATION.get(workflow_id)).send_event({"type": "approval",
    "payload": {"approved": ...}})`.

### 8. Storage

`IncidentAgent` SQLite:

*   `incident(id TEXT PRIMARY KEY, sid TEXT, title TEXT, status TEXT,
    created_at REAL)`. The status is `open`, `mitigating` or `resolved`.
*   `messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, meta TEXT,
    created_at REAL)`.
*   `findings(id INTEGER PRIMARY KEY, tool TEXT, args TEXT, result TEXT,
    created_at REAL)`.
*   `audit_log(id INTEGER PRIMARY KEY, kind TEXT, detail TEXT, created_at
    REAL)`.
*   `proposals(id TEXT PRIMARY KEY, action TEXT, params TEXT, reason TEXT,
    fingerprint TEXT, status TEXT, workflow_id TEXT, created_at REAL,
    updated_at REAL)`.
*   `workflow_steps(proposal_id TEXT, step TEXT, at REAL)`. The primary key
    is `(proposal_id, step)`.

`CheckoutService` SQLite:

*   `state(key TEXT PRIMARY KEY, value TEXT)`.
*   `deploys(version TEXT PRIMARY KEY, at REAL, author TEXT, summary TEXT)`.
*   `memory(incident_id TEXT PRIMARY KEY, title TEXT, tags TEXT,
    root_cause TEXT, remediation TEXT, resolved_at REAL)`.
*   `incident_index(id TEXT PRIMARY KEY, title TEXT, status TEXT,
    created_at REAL)`. The sidebar list uses this table.

SQL never binds `None`, because Pyodide changes it to `undefined`. The code
stores empty values as `''` or `0`.

### 9. HTTP API

| Method and path | Effect |
|---|---|
| `POST /api/incidents` `{title?}` | Makes an id (`inc-` + 8 hex), starts the `IncidentAgent`, adds it to the index |
| `GET /api/incidents` | Lists the incidents from `incident_index` |
| `GET /api/incidents/{id}` | `{incident, messages, audit, proposals, busy, llm_mode, fallback_url}` |
| `POST /api/incidents/{id}/chat` `{message}` | Runs one turn. Returns `{message}`, or 409 if busy, or 429 if rate-limited |
| `POST /api/incidents/{id}/proposals/{pid}/approve` and `/reject` | Records the decision |
| `GET /api/service` | `{version, fault, metrics: {current values}, deploys}` |
| `POST /api/service/fault` `{fault}` and `POST /api/service/reset` | Demo controls |

An error returns JSON `{error}` with status 400, 404, 409, 429 or 500. If the
request `Origin` is `http://localhost:3000`, the response gets CORS headers
for `next dev`.

### 10. UI (`web/`, Next.js App Router, `output: "export"`, Tailwind)

The UI is one page with three columns. On narrow screens, the columns
stack.

*   **Left:** the service panel. It has four metric tiles, the current
    version and the active fault. Below it are the fault buttons, Reset, and
    the incident list with "New incident".
*   **Center:** the chat. Each assistant message shows a "numbers verified"
    badge, or a warning with the unverified numbers. A proposal card shows
    the action, the arguments and the reason. While the proposal waits, the
    card shows Approve and Reject. After the decision, it shows the Workflow
    steps.
*   **Right:** the audit log, with the newest entry first. Each entry shows
    its kind and a detail that the user can expand.

The `?id=` query parameter keeps the selected incident, because a static
export has no dynamic routes. The page polls `GET /api/incidents/{id}` every
1.5 s while a turn or a Workflow runs. It polls `GET /api/service` every 3 s.

The page shows the user message immediately, before the server stores it.
The page removes this temporary copy when the poll returns the stored
message. The page compares message ids, not text.

### 11. Deployment

There are two Workers. Both use the same code.

| Worker | Command | Environment | URL |
|---|---|---|---|
| `cf-ai-oncall-copilot` | `npm run deploy` | default (`LLM_MODE=workers-ai`) | `cf-ai-oncall-copilot.risingspy007-fe3.workers.dev` |
| `cf-ai-oncall-copilot-scripted` | `npm run deploy:scripted` | `scripted` (no `AI` binding) | `cf-ai-oncall-copilot-scripted.risingspy007-fe3.workers.dev` |

The fallback Worker has no `AI` binding. Thus, it always works, also when the
Workers AI quota is empty. The main Worker has the variable `FALLBACK_URL`
with the address of the fallback Worker.

**Workers Builds (Git integration).** Each push to `main` starts a build of
the main Worker. Use these settings:

*   Build command: `npm run build`.
*   Deploy command: `npm run deploy:ci`.

Workers Builds runs `npm clean-install` only in the repository root. Thus,
the root `postinstall` script installs the dependencies of `web/`.
`deploy:ci` installs `pywrangler`. `pywrangler` puts the Python Workers SDK
into the bundle. Plain `wrangler deploy` does not do this.

## Alternatives Considered

### Alternative A: TypeScript Agents SDK (`AIChatAgent`)

*   **Pros**: WebSocket streaming, chat persistence, and built-in tool
    approval. It is the official quick start.
*   **Cons**: it is only for TypeScript.
*   **Reason for Rejection**: the author rejected it, because the backend
    must be Python. The SDK has no Python version.
    *   Cost: the message storage and the tool loop are custom code. This is
        approximately 150 lines in `copilot.py` and `agent.py`.
    *   The Workflow `wait_for_event` replaces the built-in approval UI.
        Thus, the approval continues after a restart and has a timeout. The
        in-chat approval of the SDK does not have these properties.

### Alternative B: WebSocket or SSE streaming from the Python Durable Object

*   **Pros**: tokens show while the model makes them.
*   **Cons**: the number check needs the full answer before the UI shows a
    part of it. Thus, a stream can show numbers that the check later
    removes. Hibernatable WebSockets in Python Durable Objects have little
    documentation.
*   **Reason for Rejection**: the author rejected it for these reasons.
    *   The audit log gives live progress. The UI polls it every 1.5 s, but
        only while a turn or a Workflow is active.
    *   This is approximately 40 small Durable Object reads each minute for
        each open tab.
    *   Hibernatable WebSockets can decrease this number. But there is no
        documentation for them in Python Durable Objects.

### Alternative C: Remediation as a Durable Object alarm sequence

*   **Pros**: no additional primitive.
*   **Cons**: retries, step memoization and the event wait are all custom
    code.
*   **Reason for Rejection**: the author rejected it. Workflows give durable
    steps, retries, and `wait_for_event` for the human approval.

### Alternative D: Cloudflare Pages for the UI

*   **Pros**: it agrees with the original plan.
*   **Cons**: a second deploy and a cross-origin API.
*   **Reason for Rejection**: the author rejected it. Workers static assets
    serve the export from the same Worker.

--------------------------------------------------------------------------------

## Review resolutions (goldfish pass 1)

These resolutions change the sections above. If there is a conflict, this
section is correct. The finding ids refer to `review/goldfish_review_report.md`.

### R1. Sandboxes (DC7)

*   The UI makes a random `sid` (12 hex characters) one time. It stores the
    `sid` in `localStorage`. It sends the `sid` as `X-Sandbox-Id` in each
    request.
*   The Worker refuses an id that does not match `^[a-f0-9]{12}$`.
*   The key of `CheckoutService` is `checkout-<sid>`. Thus, each visitor has
    a different simulator, incident list and memory.
*   Incident ids are not secret. Each `IncidentAgent` stores its `sid`. A
    request with a different `sid` gets 404.
*   A Rate Limiting binding (`CHAT_LIMITER`, 10 each 60 s, key `sid`)
    protects `/chat`.

### R2. Number check sources (DC2, DC3, DC4)

*   Allowed numbers come only from the values of tool results. The check
    reads them recursively.
*   The check ignores these keys: `ts`, `minute`, `at`, `resolved_at`,
    `incident_id`, `proposal_id`, `root_cause`, `title` and `reason`.
*   Numbers in other strings, for example log messages, are allowed. Tool
    arguments are never in the allowed set.
*   The allowed set also has the numbers in the messages of the user, and
    the integers 0 to 10 and 100.
*   `get_metrics` also returns `ratio_to_baseline` (1 decimal place).
    *   For `db_pool_in_use`, it returns `pool_size` and
        `pool_utilization_pct`.
    *   For `p95_latency_ms`, it returns `current_seconds` (1 decimal
        place).
    *   The system prompt tells the model to quote only these fields for
        comparisons.
*   Precision is the number of decimal places. `24` matches `23.8`. `2600`
    does not match `2643`.
*   If the rewrite also fails, the code replaces each unverified number in
    the text with `[unverified]`. The audit log keeps the raw draft. The
    rewrite request has no tools.

### R3. Chat turn robustness (DC8, IR7)

*   `async def run_turn(...)`. The callables are async.
*   An exception from `run_model` (AI error, quota, timeout) gives a
    `model_error` audit entry. The turn stops with "The model is unavailable
    right now; no actions were taken."
    *   Exception: error `4006` (daily quota) gives the scripted fallback
        (§6).
*   A tool exception goes back to the model as `{"error": ...}`. It gives a
    `tool_error` audit entry. It does not count for the schema-failure
    limit.
*   The limit counts only `ToolArgError` failures, for each tool in each
    turn. Business refusals are normal tool results. Examples are a bad
    rollback target or a proposal that is already open.
*   The parser accepts a tool call in text only in one condition. The full
    trimmed `response` must be one JSON object with the name of a known
    tool. The parser first removes a leading `<|python_tag|>`. It also
    accepts a list of these objects.
*   A maximum of 4 tool calls run for each model response. Each additional
    call gets `{"error": "too many tool calls in one step"}`.
*   `propose_remediation` returns an error if the incident has no read-only
    finding. It also returns an error if no finding shows a symptom (§5).
*   The Workflow can send a message while `busy` is set. The agent keeps
    these messages in memory and writes them after the turn. If the runtime
    removes the object from memory, the chat loses them. The audit log keeps
    them.

### R4. State machines (IR6, DC5, IR11)

Proposal status:

| From | To | Written by | Trigger |
|---|---|---|---|
| (none) | `creating` | `propose_remediation` | The row is added. Then `REMEDIATION.create` runs. If create fails, the code deletes the row |
| `creating` | `awaiting_approval` | `workflow_update` (step `report_awaiting`) | The snapshot is complete |
| `awaiting_approval` | `approving` / `rejected` | `decide` | Synchronous, before an await |
| `approving` | `applying` → `verifying` | `workflow_update` | Workflow steps |
| `verifying` | `succeeded` / `failed` | `workflow_update` | Verify result |
| `approving`/`applying` | `stale` | `workflow_update` | The compare-and-set found a change |
| `creating`/`awaiting_approval` | `expired` | `workflow_update` or the `get_state` sweep | The wait timed out, or the proposal is older than 65 min |

Only one proposal for each incident can have a non-terminal status. The
terminal statuses are `rejected`, `succeeded`, `failed`, `stale` and
`expired`. `decide` on a proposal with a different status returns `{"error":
"not awaiting approval", "status": ...}`. The Worker changes this to HTTP
409.

Incident status: `open` → `mitigating` (on approve) → `resolved` (on
succeeded). The status goes back to `open` on rejected, failed, stale or
expired.

### R5. Workflow code shape (IR1–IR5, IR10, DC1, DC6, DC10)

```python
class RemediationWorkflow(WorkflowEntrypoint):
    async def run(self, event, step):
        p = event["payload"]   # {incident_id, sid, proposal_id, action, target_version, pool_size,
                               #  expected_version, expected_fault}
        agent = lambda: self.env.INCIDENT_AGENT.getByName(p["incident_id"])
        service = lambda: self.env.CHECKOUT_SERVICE.getByName("checkout-" + p["sid"])

        @step.do("snapshot")
        async def snapshot(): return json.loads(await service().snapshot())
        snap = await snapshot()

        @step.do("report_awaiting")
        async def report_awaiting(): return await agent().workflow_update(dumps(step="report_awaiting", status="awaiting_approval", ...))
        await report_awaiting()

        try:
            evt = await step.wait_for_event("wait for approval", "approval", timeout="1 hour")
            approved = bool(evt["payload"]["approved"])
        except Exception:
            -> step.do("report_expired"); return
        if not approved: -> step.do("report_rejected"); return

        @step.do("apply", config={"retries": {"limit": 3, "delay": "2 seconds", "backoff": "exponential"}})
        async def apply(): return json.loads(await service().apply_remediation(dumps(p)))
        # apply_remediation: idempotent for each proposal_id; asks the agent if the proposal is
        # `approving`; compare-and-sets expected_version/expected_fault; a stale or not_approved
        # result becomes a NonRetryableError in the step
        ...
        @step.do("verify", config={"retries": {"limit": 4, "delay": "3 seconds", "backoff": "exponential"},
                                   "timeout": "30 seconds"})
        async def verify(): raise unless error_rate_pct < 1.0 and p95_latency_ms < 500
        try: final = await verify()
        except Exception: -> step.do("final_snapshot"), step.do("report_failed"); return
        -> step.do("report_succeeded")
```

Step functions have no parameters (IR1). Thus, the code works with or
without `python_workflows_implicit_dependencies`. `workflow_update` ignores a
`(proposal_id, step)` pair that it applied before. `remember` uses `INSERT OR
REPLACE`.

### R6. Simulator details (DC9, DC11)

*   `current` and verify read the metrics at the exact time `t`. The
    simulator uses minute buckets only for the jitter and for the history
    `points`.
*   With the 20 s decay, the expected verify results are: fail at
    approximately 0, 3 and 9 s, and pass at approximately 21 s.
*   A second `bad_deploy` writes `v2.15.0` again with a new `at`.
*   A rollback target must exist. Its `at` must be earlier than the `at` of
    the current version.
*   The tag `recent_deploy` is set if the current version is less than 30
    min old.
*   The expected fingerprint for `bad_deploy` is `{high_error_rate,
    recent_deploy, sig:npe_payment_client_v2}`. A second `bad_deploy` gets a
    score of 1.0 against the first.

### R7. `wrangler.jsonc`

```jsonc
{
  "name": "cf-ai-oncall-copilot",
  "main": "src/entry.py",
  "compatibility_date": "2026-11-01",
  "compatibility_flags": ["python_workers", "python_workflows"],
  "ai": { "binding": "AI" },
  "assets": { "directory": "./web/out", "not_found_handling": "single-page-application",
              "run_worker_first": ["/api/*"] },
  "durable_objects": { "bindings": [
    { "name": "INCIDENT_AGENT", "class_name": "IncidentAgent" },
    { "name": "CHECKOUT_SERVICE", "class_name": "CheckoutService" } ] },
  "migrations": [ { "tag": "v1", "new_sqlite_classes": ["IncidentAgent", "CheckoutService"] } ],
  "workflows": [ { "name": "remediation", "binding": "REMEDIATION", "class_name": "RemediationWorkflow" } ],
  "ratelimits": [ { "name": "CHAT_LIMITER", "namespace_id": "1001", "simple": { "limit": 10, "period": 60 } } ],
  "vars": { "LLM_MODE": "workers-ai",
            "FALLBACK_URL": "https://cf-ai-oncall-copilot-scripted.risingspy007-fe3.workers.dev" },
  "observability": { "enabled": true },
  "env": { "scripted": {
    // The same bindings without "ai", a different workflow name ("remediation-scripted"),
    // rate-limit namespace "1002", and "vars": { "LLM_MODE": "scripted" }
  } }
}
```

The `scripted` environment is for `npm run dev:scripted` and for the
fallback Worker (§11).

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Llama 3.3 calls tools badly (wrong arguments, calls in text, too many calls) | Flat schemas, a validator that converts types, a strict parser for calls in text, a limit for each tool, and a limit of 4 calls for each step (R3). V6 measures the pass rates |
| Llama proposes a fix for a healthy service | The evidence gate in code (§5). V6 measured 0/3 before the gate and 5/5 after it |
| Workers AI is not available | The `model_error` path. No actions occur (R3) |
| The Workers AI daily quota is empty | The scripted fallback in the turn, and the fallback Worker (§6, §11) |
| The tool-message format for Llama on Workers AI has no documentation (Q1) | Verified in V5. The parser accepts `tool_calls` and calls in text |
| A Workflow replay or retry repeats a side effect | Each side effect has its own `step.do`, and each side effect is idempotent (R5) |
| An approval acts on a changed state | The compare-and-set in `apply_remediation` (R5) |
| Visitors interfere on the public URL, or misuse the AI budget | A sandbox for each browser, and the rate limit (R1) |
| The Python Workers SDK is different from the 1.9.6 source that we read | V3 runs the full flow in `wrangler dev`. `db.py` accepts both cursor shapes |
| A reviewer thinks that the demo is scripted | The main Worker uses Llama by default. A badge shows each scripted answer. The README gives the V6 live pass rates |

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security**: there is no authentication (non-goal). A fix changes only
    the simulator. The model cannot see or call `set_fault`, `reset` or
    `apply_remediation`.
*   **Cost and latency**: a turn has a maximum of 8 model calls: 6 in the
    loop, 1 forced answer and 1 rewrite. Each tool result has a maximum of
    4,000 characters. The prompt gets only the last 12 stored messages.
*   **Rollback**: use `wrangler rollback`. Migration `v1` only creates
    classes.

--------------------------------------------------------------------------------

### Additional Resources

*   `concepts_brief.md` (the original plan)
*   Cloudflare documentation: Python Workers, Python Workflows, the Durable
    Objects SQLite API, and Workers AI function calling
*   `workers-runtime-sdk` 1.9.6 source (`workers/entrypoints.py`,
    `workers/rpc.py`)

### Future Improvements

*   Real telemetry adapters with the same tool interfaces.
*   A number check for each metric. Compare each quoted number only with the
    values of the metric next to it. Review finding DC2 says that this is
    fragile on free text. Try it with structured answer templates.
*   Authentication on the approval endpoint, with a record of the approver.
*   Vectorize embeddings for similarity, when the memory is too large for a
    table scan.
*   Voice input through Realtime.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Target Files to Create/Modify

#### 1. `wrangler.jsonc` (Modify)

*   **Type / Scope**: Build configuration.
*   **Proposed Changes**: the configuration in R7.

#### 2. `pyproject.toml` (Create)

*   **Type / Scope**: Python project metadata for `pywrangler` and `uv`
    users. It has no runtime dependencies.

#### 3. `src/entry.py` (Create)

*   **Type / Scope**: Worker entrypoint.
*   **Proposed Changes**: `Default(WorkerEntrypoint).fetch` routes the
    requests in §9. It exports `IncidentAgent`, `CheckoutService` and
    `RemediationWorkflow` again.

#### 4. `src/agent.py` (Create)

*   **Type / Scope**: Durable Object.
*   **Proposed Changes**: `IncidentAgent` with the schema in §8. Its RPC
    methods are `init`, `chat`, `get_state`, `decide`, `proposal_status` and
    `workflow_update`. It makes the `Copilot` callables and the quota
    fallback (§6).

#### 5. `src/service.py` (Create)

*   **Type / Scope**: Durable Object.
*   **Proposed Changes**: `CheckoutService` with `status`, `tool`,
    `snapshot`, `validate`, `apply_remediation`, `set_fault`, `reset`,
    `remember`, `index_incident` and `list_incidents`.

#### 6. `src/remediation.py` (Create)

*   **Type / Scope**: Python Workflow.
*   **Proposed Changes**: `RemediationWorkflow.run` as in §7 and R5.

#### 7. `src/sim.py`, `src/tools.py`, `src/guardrails.py`, `src/memory.py`, `src/ops.py`, `src/copilot.py`, `src/scripted.py`, `src/prompts.py`, `src/db.py` (Create)

*   **Type / Scope**: Pure Python modules. Only `db.py` imports `js` or
    `pyodide`, for the cursor conversion.
*   **Proposed Changes**: as in §3 to §6.

#### 8. `tests/` (Create)

*   **Type / Scope**: `unittest` (39 tests).
*   **Proposed Changes**:
    *   Fault tables, decay, and deterministic values.
    *   Validator cases: string integers, bool refusal, enums, ranges and
        unknown keys.
    *   Number check cases from CUJ 6.
    *   Evidence gate cases from CUJ 7.
    *   Similarity threshold and order.
    *   The `Copilot` loop with `ScriptedModel`.
    *   A model with malformed calls (one retry, then stop), a model that
        writes its tool call in `response`, and the rewrite path.

#### 9. `web/` (Create)

*   **Type / Scope**: Next.js app (`create-next-app`, TypeScript, Tailwind).
*   **Proposed Changes**: `next.config.ts` (`output: "export"`),
    `app/page.tsx` (the UI in §10), `lib/api.ts` (fetch wrappers;
    `NEXT_PUBLIC_API_BASE` is `""` by default), and the components
    `ServicePanel`, `IncidentList`, `ChatMessage`, `ProposalCard` and
    `AuditLog`.

#### 10. `package.json` (Create, root)

*   **Type / Scope**: scripts. `wrangler` is a pinned dev dependency.

| Script | Function |
|---|---|
| `postinstall` | Install the dependencies of `web/` |
| `setup` | Make `.venv` with `uv` and `pywrangler` |
| `build` | Build `web/out` |
| `dev` / `dev:scripted` | Build, then `pywrangler dev` (default or `scripted` environment) |
| `deploy` / `deploy:scripted` | Build, then `pywrangler deploy` (main or fallback Worker) |
| `deploy:ci` | Install `pywrangler`, then deploy (Workers Builds) |
| `test` | Run the unit tests |
| `check:llama` | Run the live Llama check (V6) |

#### 11. `README.md`, `PROMPTS.md` (Create)

*   **Type / Scope**: Documentation.
*   **Proposed Changes**: the README has the live and fallback URLs, a
    summary and the architecture diagram. It also has the local and deploy
    steps, the demo walkthrough (CUJ 1 to 4), the guardrails and the
    trade-offs. `PROMPTS.md` records each AI prompt and a summary of the
    result.

### Verification Plan

*   **V1 Build**: `npm run build` makes `web/out/index.html`.
*   **V2 Unit tests**: `npm test`. All 39 tests pass on CPython 3.13.
*   **V3 Local end-to-end (scripted)**: run `npm run dev:scripted`. Then do
    CUJ 1 to 5 with `curl`. Check these items:
    *   The proposal and Workflow statuses.
    *   The verify retries during the decay.
    *   The memory match for the second incident.
    *   The rejection path.
*   **V4 Lint**: `python3 -m py_compile src/*.py`, and the type check of
    `next build`.
*   **V5 Live**: log in with `wrangler login` and deploy. Then do CUJ 1 and
    CUJ 4 with Llama 3.3. Confirm the Q1 tool-message format. Record the
    results in the README.
*   **V6 Live reliability**: run `npm run check:llama` (`scripts/llama_check.py`).
    *   It does each scenario 3 times by default: no fault, `bad_deploy`,
        `db_pool_exhaustion` and `latency_spike`.
    *   A run passes if the proposal is the expected fix, or no proposal for
        the healthy case.
    *   It reads the `llm_mode` of each answer. It stops if the server is in
        scripted mode or the turn used the quota fallback.
    *   Results (2026-11-12): all three faults passed 3/3. The healthy case
        failed 0/3 before the evidence gate, and passed 5/5 after it.

--------------------------------------------------------------------------------

<!-- Provenance: Indicates initial structural drafting via EGM. -->

*Drafted with EGM*
