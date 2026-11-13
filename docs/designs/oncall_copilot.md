# On-call Incident Copilot

*   **Author:** bandhanp1804
*   **Status:** Reviewed (goldfish pass 1 resolved; see `review/goldfish_review_report.md`)
*   **Date:** 2026-11-12

--------------------------------------------------------------------------------

## Overview

`cf_ai_oncall_copilot` is a chat assistant for an on-call engineer. It runs
entirely on Cloudflare. The engineer opens an incident and writes something
like "checkout is throwing 500s". The copilot investigates with read-only
tools, compares the incident with past ones, and proposes one fix. The fix
runs only after the engineer approves it, and it runs as a durable Workflow
that snapshots, applies, verifies and reports.

The app ships with a simulated checkout API, so a reviewer can run the full
demo with no real infrastructure. The simulator produces metrics, logs and
deploy events, and has three faults that can be triggered from the UI.

Stack: a Python Worker (Durable Objects, a Python Workflow, Workers AI through
the `AI` binding) and a Next.js UI built as a static export and served from the
same Worker's static assets.

## The Problem

On-call engineers lose the first minutes of an incident gathering the same
data: error rate, latency, recent deploys and matching log lines. They then
check whether the team has seen the incident before. An LLM can do the
gathering, but in incident response two failure modes are unacceptable: the
model changing production on its own, and the model quoting numbers that no
system reported.

The copilot does the gathering with tools that can only read. It treats the one
action that changes the service as a separate, durable, human-approved
Workflow. In code, it checks every number in the model's answer against the
tool results before showing that answer.

## Goals

*   G1. Triggering the `bad_deploy` fault and asking "why is checkout failing?"
    yields an answer that names the bad version (`v2.15.0`) and quotes an
    error rate that appears in a `get_metrics` result.
*   G2. Apart from the demo controls (`set_fault`, `reset`, which the UI
    calls and the model cannot), no code path changes simulator state except
    `RemediationWorkflow`. `apply_remediation` refuses unless the owning
    `IncidentAgent` reports the proposal as `approving`, which only a human
    Approve click sets.
*   G3. After the first `bad_deploy` incident is resolved, a second
    `bad_deploy` incident's answer says it matches a past incident that was
    fixed by rolling back. The answer uses `find_similar_incidents` output.
*   G4. Every tool call, validation error, proposal, approval, rejection,
    Workflow step and number-check result is written to the incident's audit
    log and shown in the UI.
*   G5. A malformed tool call (unknown tool, missing or wrong-typed argument)
    never raises out of the chat request. The model gets one corrective error
    per tool per turn; a second bad call to the same tool in the same turn
    ends the tool loop with a message to the user.
*   G6. `python3 -m unittest discover -s tests` passes on stock CPython 3.13
    with no installed packages.

## Non-Goals

*   Real telemetry, real deploy systems, or real paging integrations.
*   Authentication or authorization on any endpoint, including approval.
*   Token-by-token streaming of model output (see Alternatives B).
*   Voice input through Realtime, and Vectorize. Both are stretch goals.
*   Multi-service support. The simulator models one service, `checkout`.

## Open Questions

*   **Q1: Llama 3.3 tool-message format on Workers AI.** The docs do not show
    how this model expects tool results to be sent back. The design sends
    `{"role": "tool", "name": ..., "content": ...}` after an assistant
    message that restates the call. The parser also accepts a tool call
    written as JSON in the `response` text. This must be checked on the first
    deploy (verification step V5).
*   **Q2: Workers AI in local dev.** The `AI` binding always calls the remote
    service, so `wrangler dev` needs `wrangler login`. Without it, local runs
    use `LLM_MODE=scripted` (see Technical Plan §6).

## Critical User Journeys (CUJs)

### CUJ 1: Diagnose a bad deploy

*   **Given**: the simulator is healthy on `v2.14.1`, and no past incidents
    are stored.
*   **When**: the user clicks "Trigger fault: bad deploy", creates an
    incident, and sends "checkout is throwing 500s, why?"
*   **Then**: the audit log shows `get_metrics`, `get_recent_deploys` and
    `search_logs` calls. The reply names `v2.15.0`, quotes an error rate taken
    from a tool result, carries a "numbers verified" badge, and ends with a
    proposal card: "Roll back to v2.14.1".

### CUJ 2: Approve and remediate

*   **Given**: CUJ 1 ended with a pending proposal.
*   **When**: the user clicks Approve.
*   **Then**: the Workflow's steps show in order (snapshot, wait for approval,
    apply, verify, report). Verify may retry while the error rate falls.
    The incident becomes `resolved`, the chat gets a closing message with
    the verified error rate, and the incident is stored in service memory.

### CUJ 3: Reject a proposal

*   **Given**: a pending proposal.
*   **When**: the user clicks Reject.
*   **Then**: the Workflow ends without calling the apply step. The proposal
    is `rejected`, the audit log has a `rejection` entry, the fault stays
    active, and the user can keep chatting.

### CUJ 4: Recall a past fix

*   **Given**: CUJ 2 has finished once, and the service is healthy.
*   **When**: the user triggers bad deploy again, opens a new incident and
    asks what is wrong.
*   **Then**: `find_similar_incidents` returns the earlier incident with a
    score of at least 0.5 (1.0 expected: same tags), and the reply says it was fixed before by rolling
    back.

### CUJ 5: Wrong fix does not hide the fault

*   **Given**: the `db_pool_exhaustion` fault is active.
*   **When**: a `rollback_deploy` proposal is approved, which can happen if
    the model picks the wrong fix.
*   **Then**: apply succeeds, verify exhausts its retries, the proposal ends
    `failed`, the incident stays open, and the chat says that verification
    failed.

### CUJ 6: Model quotes an invented number

*   **Given**: the model's draft answer contains "error rate is 40%", and no
    tool result contains 40.
*   **When**: the number check runs.
*   **Then**: the model gets one rewrite request listing the unverified
    numbers. If the rewrite still fails the check, the reply is shown with a
    warning badge listing the numbers, and a `number_check` failure goes to
    the audit log.

## Technical Plan (Architecture)

```text
 Browser (Next.js static export)
   │  fetch /api/*  (poll GET /api/incidents/{id} every 1.5 s while busy)
   ▼
 Default (Python WorkerEntrypoint)      ── serves web/out via ASSETS for non-/api paths
   │ RPC (JSON strings)                 │ RPC
   ▼                                    ▼
 IncidentAgent (DO, 1 per incident)   CheckoutService (DO, singleton "checkout")
   SQLite: incident, messages,          SQLite: state, deploys, memory (past incidents),
           findings, audit_log,                 incident_index
           proposals                     simulator: metrics/logs from (state, now)
   tool loop ──► Workers AI (Llama 3.3)   read-only methods + apply_remediation()
   read-only tools ──────────────────────►        ▲
   propose_remediation ──► REMEDIATION.create()   │ only caller of apply_remediation
                                     │            │
                                     ▼            │
                     RemediationWorkflow (Python Workflow)
                       snapshot → wait_for_event("approval") → apply → verify → report
                       report ──► IncidentAgent.workflow_update / CheckoutService.remember
```

### 1. Components

*   **`Default` (Worker entrypoint).** Routes `/api/*` to the Durable Objects
    and passes every other path to `env.ASSETS`. It holds no state.
*   **`IncidentAgent` (Durable Object, keyed by incident id).** Owns the
    conversation and the tool loop, the incident's findings, proposals and
    audit log. Only one chat turn runs at a time; a second `chat` call while a
    turn is running returns `{"error": "busy"}` and the Worker returns HTTP 409.
    Reads (`get_state`) are allowed during a turn. They interleave because
    the turn awaits outbound I/O, which opens the object's input gate.
*   **`CheckoutService` (Durable Object, singleton name `checkout`).** Owns the
    simulator state and the memory of past incidents. Its read methods compute
    metrics and logs from stored state and the current time. Its one write
    method, `apply_remediation`, is called only by `RemediationWorkflow`.
    `set_fault` and `reset` are demo controls called from the UI's fault
    buttons. They are not exposed as tools.
*   **`RemediationWorkflow` (Python Workflow).** One instance per proposal,
    created when the model calls `propose_remediation`.

Memory lives in `CheckoutService`, not `IncidentAgent`, because one incident's
object cannot query another's storage. The service's history is the natural
owner of "how was this fixed before".

### 2. RPC boundary

All Durable Object RPC methods take and return JSON strings
(`json.dumps`/`json.loads`). The Python Workers SDK converts dicts for binding
calls, but values arriving in a Durable Object method can be `JsProxy`
objects. Strings cross the FFI with no conversion rules to get wrong. Workflow
`params` and event payloads are passed as dicts, because the SDK wraps
`create`, `send_event` and `run(event, step)` with `python_to_rpc` /
`python_from_rpc`.

### 3. Simulator (`src/sim.py`, pure Python)

State: `version`, `fault` (`none | bad_deploy | db_pool_exhaustion |
latency_spike`), `fault_started_at`, `remediated_at`, `db_pool_size` (50).
Seed deploys: `v2.14.0` (two days ago) and `v2.14.1` (one day ago).
Triggering `bad_deploy` appends `v2.15.0` ("Migrate payment client to
PaymentClientV2") at the current time and sets `version`.

Metrics are computed per minute bucket from `(state, t)`. Jitter comes from a
deterministic hash of `(metric, minute)`, so repeated calls in the same minute
return the same numbers. This keeps the number check stable.

| Fault | error_rate_pct | p95_latency_ms | rps | db_pool_in_use | Log signature | Fix |
|---|---|---|---|---|---|---|
| none | 0.3–0.6 | 180–220 | 420–480 | 18–26 of 50 | INFO only | — |
| bad_deploy | 22–26 | 320–380 | 420–480 | 18–26 | `NullPointerException at PaymentClientV2.authorize` (HTTP 500, version v2.15.0) | `rollback_deploy` to the previous version |
| db_pool_exhaustion | 8–12 | 2600–3000 | 520–600 | 50 of 50 | `Timeout acquiring DB connection after 30000ms (pool 50/50 in use)` | `scale_db_pool` to more than 50 |
| latency_spike | 2–3 | 3900–4300 | 420–480 | 30–38 | `payments-gateway responded in 3900ms (timeout 4000ms)` | `failover_payment_provider` |

The fault reaches its full effect 10 s after `fault_started_at` (short, so the demo responds quickly). After a
matching remediation, values move linearly back to baseline over 20 s from
`remediated_at`, then the fault is cleared. A remediation that does not match
the fault sets `remediated_at` but does not reduce the fault's effect.

Values are rounded when produced: percentages to 1 decimal place, latency
and counts to integers.

### 4. Tools

Every tool schema is flat: top-level scalar properties only, at most four
properties, and enums for closed sets. Schemas are written once in
`src/tools.py`, sent to the model, and used by the validator.

| Tool | Arguments | Returns |
|---|---|---|
| `get_metrics` | `metric` enum (`error_rate_pct`, `p95_latency_ms`, `rps`, `db_pool_in_use`), `window_minutes` int 5–60 (default 15) | `{metric, unit, points: [{minute, value}], current, baseline, max}` |
| `search_logs` | `query` string (optional, substring), `level` enum (`ERROR`, `WARN`, `INFO`, optional), `limit` int 1–50 (default 20) | `{matches, total_matching, lines: [{ts, level, msg}]}` over the last 15 minutes |
| `get_recent_deploys` | `limit` int 1–10 (default 5) | `{current_version, deploys: [{version, at, author, summary}]}` |
| `find_similar_incidents` | `query` string (optional) | `{fingerprint, matches: [{incident_id, title, score, root_cause, remediation, resolved_at}]}` |
| `propose_remediation` | `action` enum (`rollback_deploy`, `scale_db_pool`, `failover_payment_provider`), `target_version` string (needed for rollback), `pool_size` int 60–200 (needed for scale), `reason` string | `{proposal_id, status: "pending_approval"}`, or an error |

`propose_remediation` changes no service state. It checks the action's
arguments (a rollback target must be an existing version older than the
current one), stores a proposal, and creates the Workflow instance. It
rejects a new proposal while another is pending or running.

**Validation (`src/guardrails.py: validate_args`).** The validator checks
the required properties, the type (`string`, `integer`, with `bool` not
accepted as an integer), `enum`, `minimum`/`maximum`, and strips unknown
properties. Integers sent as strings (`"15"`) are converted, because Llama
often sends them that way. Defaults are filled in. On failure the tool result
is `{"error": "<what is wrong>", "expected": <schema>}`. For each turn, a
counter tracks failures per tool name. A second failure for the same tool
ends the loop with "I could not call <tool> correctly; here is what I found
so far", and writes a `tool_error` audit entry.

**Similarity (`src/memory.py`).** A fingerprint is a set of tags computed in
code from current telemetry, not from model text: `high_error_rate`
(error_rate_pct > 5), `high_latency` (p95 > 1000), `pool_saturated` (in use
≥ 95% of the pool size), `recent_deploy` (newest deploy younger than 30
minutes), and `sig:<log signature id>` for the most frequent ERROR signature.
The score is the Jaccard index of the current tags and the stored ones, plus
0.1 if the optional `query` shares a word of four or more letters with the
stored title. The score is capped at 1.0 and rounded to 2 places. Matches
need a score of at least 0.5; the top 3 are returned.

### 5. Chat turn (`src/copilot.py`, pure Python)

`Copilot.run_turn(user_text)` takes injected callables
(`run_model(messages, tools) -> dict`, `execute_tool(name, args) -> dict`,
`audit(kind, detail)`), so tests can run it under CPython.

1.  Build the messages: the system prompt (`src/prompts.py`), the last 12
    stored user and assistant messages, and the new user message.
2.  Loop, at most 6 model calls:
    *   Call the model. Parse tool calls from `tool_calls` (each with `name`
        and `arguments` as a dict or a JSON string), or from a JSON object
        in `response` with `name` and `parameters`/`arguments`.
    *   If there are no tool calls, the `response` text is the draft answer.
        Leave the loop.
    *   Otherwise validate and run each call, and record findings and an
        audit entry. Append the assistant tool-call message and one `tool`
        message per call, with the result as JSON truncated to 4,000
        characters.
3.  If the loop hit 6 calls with no answer, ask once for an answer with
    tools disabled.
4.  Number check (below). On failure, ask for one rewrite, then check again.
5.  Store the final assistant message with metadata: `number_check` result,
    and `proposal_id` if one was created in this turn.

**Number check (`src/guardrails.py: check_numbers`).** The check pulls
numbers from the answer with a regex. It skips numbers inside version strings
(`v2.15.0`) and incident or proposal ids, and removes `,` and a trailing `%`.
The allowed set is every number in the JSON of this incident's findings,
every number in the user's messages, and the integers 0 to 10 (for counts
and list markers). A number passes if it matches an allowed value exactly,
or if rounding an allowed value to its precision gives it (so `24` matches
`23.8`, and `23.8` matches `23.84`). The check returns
`{ok, unverified: [..]}`.

### 6. Model modes

`LLM_MODE` (wrangler var) is `workers-ai` by default. It calls
`env.AI.run("@cf/meta/llama-3.3-70b-instruct-fp8-fast", {messages, tools,
max_tokens: 1024, temperature: 0.2})`. `scripted` uses `ScriptedModel`
(`src/scripted.py`), a fixed planner. It calls `get_metrics`,
`get_recent_deploys`, `search_logs` and `find_similar_incidents`, then
proposes the matching fix and writes its answer only from tool results.
`ScriptedModel` is the test double for `Copilot`. It also lets
`wrangler dev` run the full demo without a Cloudflare login. The UI shows a
"scripted model" badge whenever this mode is active.

### 7. Remediation Workflow (`src/remediation.py`)

Params: `{incident_id, proposal_id, action, target_version, pool_size}`.

1.  `snapshot` (`step.do`): `CheckoutService.snapshot()` returns the version,
    fault and current metrics. The step reports `step_done` to the incident.
2.  `step.wait_for_event("wait for approval", "approval", timeout="1 hour")`.
    If the payload is `approved: false`, report `rejected` and return. On
    timeout (exception), report `expired` and return.
3.  `apply` (`step.do`, retries 3, 2 s exponential):
    `CheckoutService.apply_remediation(...)`.
4.  `verify` (`step.do`, retries 5, 5 s exponential, 30 s timeout): read the
    metrics. Raise unless `error_rate_pct < 1.0` and `p95_latency_ms < 500`.
    If retries run out, report `failed` with the last snapshot and return.
5.  `report` (`step.do`): `IncidentAgent.workflow_update(succeeded, …)`.
    The agent marks the incident `resolved` and posts a closing message
    built from the verify snapshot. It also calls `CheckoutService.remember`
    with the fingerprint taken at proposal time, the root cause from the
    proposal reason, and the action.

Each step reports to `IncidentAgent.workflow_update`, which writes a
`workflow_step` audit entry and updates the proposal's status. The UI
displays those statuses.

Approval path: `POST /api/incidents/{id}/proposals/{pid}/approve` →
`IncidentAgent.decide(pid, approved)`. It checks that the proposal is
`pending`, writes an `approval` or `rejection` audit entry, then calls
`(await env.REMEDIATION.get(workflow_id)).send_event({"type": "approval",
"payload": {"approved": ...}})`.

### 8. Storage

`IncidentAgent` SQLite:

*   `incident(id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at REAL)`
    with status `open | mitigating | resolved`.
*   `messages(id INTEGER PRIMARY KEY, role TEXT, content TEXT, meta TEXT,
    created_at REAL)`.
*   `findings(id INTEGER PRIMARY KEY, tool TEXT, args TEXT, result TEXT,
    created_at REAL)`.
*   `audit_log(id INTEGER PRIMARY KEY, kind TEXT, detail TEXT, created_at
    REAL)`.
*   `proposals(id TEXT PRIMARY KEY, action TEXT, params TEXT, reason TEXT,
    fingerprint TEXT, status TEXT, workflow_id TEXT, created_at REAL,
    updated_at REAL)`.

`CheckoutService` SQLite:

*   `state(key TEXT PRIMARY KEY, value TEXT)`.
*   `deploys(version TEXT PRIMARY KEY, at REAL, author TEXT, summary TEXT)`.
*   `memory(incident_id TEXT PRIMARY KEY, title TEXT, tags TEXT,
    root_cause TEXT, remediation TEXT, resolved_at REAL)`.
*   `incident_index(id TEXT PRIMARY KEY, title TEXT, status TEXT,
    created_at REAL)`. This feeds the sidebar list.

SQL never binds `None`, because Pyodide maps it to `undefined`. Empty values
are stored as `''` or `0`.

### 9. HTTP API

| Method and path | Effect |
|---|---|
| `POST /api/incidents` `{title?}` | Creates an id (`inc-` + 8 hex), initializes the `IncidentAgent`, indexes it |
| `GET /api/incidents` | Lists incidents from `incident_index` |
| `GET /api/incidents/{id}` | `{incident, messages, audit, proposals, busy, llm_mode}` |
| `POST /api/incidents/{id}/chat` `{message}` | Runs one turn; `{message}` or 409 if busy |
| `POST /api/incidents/{id}/proposals/{pid}/approve` and `/reject` | Decision |
| `GET /api/service` | `{version, fault, metrics: {current values}, deploys}` |
| `POST /api/service/fault` `{fault}` / `POST /api/service/reset` | Demo controls |

Errors return JSON `{error}` with 400, 404, 409 or 500. When the request
`Origin` is `http://localhost:3000`, responses get CORS headers, for
`next dev`.

### 10. UI (`web/`, Next.js App Router, `output: "export"`, Tailwind)

The UI is one page with three columns that stack on narrow screens.

*   **Left:** the service panel with four metric tiles, the current version
    and the active fault. Below it are the fault buttons and Reset, then the
    incident list with "New incident".
*   **Center:** the chat. An assistant message shows a "numbers verified"
    badge or a warning that lists unverified numbers. A proposal card shows
    the action, the arguments and the reason, with Approve and Reject while
    pending, and Workflow step progress after that.
*   **Right:** the audit log, newest first, with each entry's kind and a
    collapsible detail.

The selected incident is kept in `?id=`, because a static export has no
dynamic routes. The page polls `GET /api/incidents/{id}` every 1.5 s while a
turn or Workflow is running, and `GET /api/service` every 3 s.

## Alternatives Considered

### Alternative A: TypeScript Agents SDK (`AIChatAgent`)

*   **Pros**: WebSocket streaming, chat persistence, and built-in tool
    approval. It is the official quick start.
*   **Cons**: TypeScript only.
*   **Reason for Rejection**: Rejected by author: the backend must be Python,
    and the SDK has no Python version. The cost: message persistence and the
    tool loop are written by hand (about 150 lines in `copilot.py` and
    `agent.py`), and the built-in approval UI is replaced by the Workflow's
    `wait_for_event`. The approval then survives restarts and has a timeout,
    which the SDK's in-chat approval does not provide.

### Alternative B: WebSocket or SSE streaming from the Python Durable Object

*   **Pros**: tokens show up as the model generates them.
*   **Cons**: the number check must run on the whole answer before it is
    shown, so streaming the answer text would show unchecked numbers.
    Hibernatable WebSockets in Python Durable Objects have little
    documentation.
*   **Reason for Rejection**: Rejected by author: the number check needs the
    whole answer before any of it is shown, so streaming tokens would only
    stream text that might be redacted. Live progress comes from the audit
    log: the UI polls it every 1.5 s, and only while a turn or Workflow is
    active. That is about 40 cheap Durable Object reads per minute per open
    tab. Hibernatable WebSockets would cut that, but are undocumented for
    Python Durable Objects.

### Alternative C: Remediation as a Durable Object alarm sequence

*   **Pros**: no extra primitive.
*   **Cons**: retries, step memoization and waiting for an event would all
    have to be written by hand.
*   **Reason for Rejection**: Rejected by author: Workflows provide durable
    steps, retries, and `wait_for_event` for the human approval.

### Alternative D: Cloudflare Pages for the UI

*   **Pros**: matches the original plan.
*   **Cons**: a second deploy and a cross-origin API.
*   **Reason for Rejection**: Rejected by author: Workers static assets serve
    the export from the same Worker.

--------------------------------------------------------------------------------

## Review resolutions (goldfish pass 1)

These amend the sections above. Where they conflict, this section wins.
Finding ids refer to `review/goldfish_review_report.md`.

### R1. Sandboxes (DC7)

The UI generates a random `sid` (12 hex characters) once, stores it in
`localStorage`, and sends it as `X-Sandbox-Id` on every request. The Worker
rejects ids that do not match `^[a-f0-9]{12}$`. `CheckoutService` is keyed
`checkout-<sid>`, so each visitor has their own simulator, incident list and
memory. Incident ids are not secret. Each `IncidentAgent` stores its `sid`,
and requests with a different `sid` get 404. A Rate Limiting binding
(`CHAT_LIMITER`, 10 per 60 s, keyed by `sid`) guards `/chat`.

### R2. Number check sources (DC2, DC3, DC4)

*   Allowed numbers come only from tool result values, walked recursively.
    Keys `ts`, `minute`, `at`, `resolved_at`, `incident_id`, `proposal_id`,
    `root_cause`, `title` and `reason` are skipped. Numbers inside other
    strings, such as log messages, are allowed. Tool args are never included.
*   Allowed numbers also include the numbers in the user's messages and the
    integers 0 to 10 and 100.
*   `get_metrics` also returns `ratio_to_baseline` (1 decimal place). For
    `db_pool_in_use` it returns `pool_size` and `pool_utilization_pct`. For
    `p95_latency_ms` it returns `current_seconds` (1 decimal place). The
    system prompt tells the model to quote only these fields.
*   Precision means decimal places. `24` matches `23.8`, and `2600` does not
    match `2643`.
*   If the rewrite also fails, every unverified number in the shown text is
    replaced with `[unverified]`. The audit log keeps the raw draft. The
    rewrite request runs with no tools.

### R3. Chat turn robustness (DC8, IR7)

*   `async def run_turn(...)`. The injected callables are async.
*   `run_model` exceptions (AI errors, quota, timeouts) are audited as
    `model_error`. The turn ends with "The model is unavailable right now;
    no actions were taken."
*   Tool exceptions are returned to the model as `{"error": ...}` and audited
    as `tool_error`. They do not count toward the schema-failure cap.
*   The cap counts only `ToolArgError` failures per tool per turn. Business
    rejections, such as a bad rollback target or a proposal already open,
    are ordinary tool results.
*   Tool calls in text are accepted only if the whole trimmed `response`
    (with a leading `<|python_tag|>` removed) parses as a single JSON object
    whose `name` is a known tool. A list of such objects is also accepted.
*   At most 4 tool calls run per model response. Extra calls get
    `{"error": "too many tool calls in one step"}`.
*   `propose_remediation` returns an error unless the incident already has at
    least one read-only finding.
*   Messages from the Workflow that arrive while `busy` is set are queued in
    memory and written after the turn. If the object is evicted they are
    lost from chat but stay in the audit log.

### R4. State machines (IR6, DC5, IR11)

Proposal status:

| From | To | Written by | Trigger |
|---|---|---|---|
| (none) | `creating` | `propose_remediation` | Row inserted; then `REMEDIATION.create`. If create fails, the row is deleted |
| `creating` | `awaiting_approval` | `workflow_update` (step `report_awaiting`) | Snapshot done |
| `awaiting_approval` | `approving` / `rejected` | `decide` | Synchronous, before any await |
| `approving` | `applying` → `verifying` | `workflow_update` | Workflow steps |
| `verifying` | `succeeded` / `failed` | `workflow_update` | Verify outcome |
| `approving`/`applying` | `stale` | `workflow_update` | Compare-and-set mismatch |
| `creating`/`awaiting_approval` | `expired` | `workflow_update` or `get_state` sweep | Wait timeout, or older than 65 min |

Only one proposal per incident may be in a non-terminal status. The
terminal statuses are `rejected`, `succeeded`, `failed`, `stale` and
`expired`. `decide` on a proposal in any other status returns
`{"error": "not awaiting approval", "status": ...}`, and the Worker turns
that into HTTP 409.

Incident status: `open` → `mitigating` (on approve) → `resolved` (on
succeeded). It returns to `open` on rejected, failed, stale or expired.

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
        # apply_remediation: idempotent per proposal_id; asks the agent that the proposal is
        # `approving`; compare-and-sets expected_version/expected_fault; raises NonRetryableError
        # (as an {"error": "stale"|"not_approved"|...} result converted to the exception in the step)
        ...
        @step.do("verify", config={"retries": {"limit": 4, "delay": "3 seconds", "backoff": "exponential"},
                                   "timeout": "30 seconds"})
        async def verify(): raise unless error_rate_pct < 1.0 and p95_latency_ms < 500
        try: final = await verify()
        except Exception: -> step.do("final_snapshot"), step.do("report_failed"); return
        -> step.do("report_succeeded")
```

Step functions take no parameters (IR1), so the code works whether or not
`python_workflows_implicit_dependencies` is on. `workflow_update` ignores a
`(proposal_id, step)` pair it has already applied. `remember` uses
`INSERT OR REPLACE`.

### R6. Simulator details (DC9, DC11)

*   `current` and verify read metrics at the exact `t`. Per-minute buckets are
    used only for jitter and for the history `points`.
*   With the 20 s decay, verify is expected to fail at about 0, 3 and 9 s and
    pass at about 21 s.
*   Triggering `bad_deploy` again upserts `v2.15.0` with a fresh `at`.
*   A rollback target must exist and must have an earlier `at` than the
    current version.
*   `recent_deploy` is set when the current version was deployed less than
    30 min ago.
*   Expected fingerprint for a `bad_deploy`:
    `{high_error_rate, recent_deploy, sig:npe_payment_client_v2}`. A second
    `bad_deploy` scores 1.0 against the first.

### R7. Literal `wrangler.jsonc`

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
  "vars": { "LLM_MODE": "workers-ai" },
  "observability": { "enabled": true }
}
```

`LLM_MODE=scripted` is set only through `.dev.vars` for local runs.

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Llama 3.3 calls tools badly (wrong args, calls in text, too many calls) | Flat schemas, a validator that converts types, a strict text-call parser, a per-tool cap, and a 4-calls-per-step cap (R3). V6 measures pass rates |
| Workers AI is down or out of quota | `model_error` path; no actions taken (R3) |
| Tool-message format for Llama on Workers AI is undocumented (Q1) | Verified in V5. The parser accepts both `tool_calls` and calls written in text |
| Workflow replay or retries repeat side effects | Every side effect is in its own `step.do`, and the side effects are idempotent (R5) |
| Approval acts on changed state | Compare-and-set in `apply_remediation` (R5) |
| Visitors interfere on the public URL; AI cost abuse | Per-browser sandboxes and the rate limit (R1) |
| Python Workers SDK behaves differently from the 1.9.6 source read here | V3 runs the full flow in `wrangler dev`, and `db.py` handles both cursor shapes |
| The demo is judged to be scripted | Scripted mode is local-only and off by default, and shows a badge. The README reports V6 live pass rates |

--------------------------------------------------------------------------------

## Non-Functional Requirements

*   **Security**: there is no auth (non-goal). Remediation affects only the
    simulator. The model never sees or calls `set_fault`, `reset` or
    `apply_remediation`.
*   **Cost and latency**: at most 8 model calls per turn (6 in the loop, 1
    forced answer, 1 rewrite). Tool results are truncated to 4,000
    characters each. Only the last 12 stored messages go into the prompt.
*   **Rollback**: `wrangler rollback`. Migration `v1` only creates classes.

--------------------------------------------------------------------------------

### Additional Resources

*   `concepts_brief.md` (the original plan)
*   Cloudflare docs: Python Workers, Python Workflows, Durable Objects SQLite
    API, Workers AI function calling
*   `workers-runtime-sdk` 1.9.6 source (`workers/entrypoints.py`,
    `workers/rpc.py`)

### Future Improvements

*   Real telemetry adapters behind the same tool interfaces.
*   Number check per metric: match each quoted number against the values of
    the metric named next to it, not the whole incident's set. (Contested
    in review DC2 as brittle on free prose; worth trying with structured
    answer templates.)
*   Auth on the approval endpoint, with the approver recorded.
*   Vectorize embeddings for similarity once memory outgrows a table scan.
*   Voice input through Realtime.

--------------------------------------------------------------------------------

## Detailed Implementation Plan

### Target Files to Create/Modify

#### 1. `wrangler.jsonc` (Modify)

*   **Type / Scope**: Build configuration
*   **Proposed Changes**: name `cf-ai-oncall-copilot`, `main: src/entry.py`,
    flags `python_workers` and `python_workflows`, `ai` binding `AI`,
    `assets` `{directory: ./web/out, binding: ASSETS,
    not_found_handling: single-page-application, run_worker_first:
    ["/api/*"]}`, Durable Object bindings `INCIDENT_AGENT` and
    `CHECKOUT_SERVICE`, migration `v1` with `new_sqlite_classes`, workflow
    `remediation` with binding `REMEDIATION`, `vars.LLM_MODE = "workers-ai"`.

#### 2. `pyproject.toml` (Create)

*   **Type / Scope**: Python project metadata for `pywrangler`/`uv` users.
    It has no runtime dependencies.

#### 3. `src/entry.py` (Create)

*   **Type / Scope**: Worker entrypoint
*   **Proposed Changes**: `Default(WorkerEntrypoint).fetch` routes the table
    in §9. It re-exports `IncidentAgent`, `CheckoutService` and
    `RemediationWorkflow`.

#### 4. `src/agent.py` (Create)

*   **Type / Scope**: Durable Object
*   **Proposed Changes**: `IncidentAgent` with the schema in §8 and the RPC
    methods `init(json)`, `chat(json)`, `get_state()`, `decide(json)` and
    `workflow_update(json)`. It also builds the `Copilot` callables.

#### 5. `src/service.py` (Create)

*   **Type / Scope**: Durable Object
*   **Proposed Changes**: `CheckoutService` with `status()`, `metrics(json)`,
    `logs(json)`, `deploys(json)`, `fingerprint()`, `similar(json)`,
    `snapshot()`, `apply_remediation(json)`, `set_fault(json)`, `reset()`,
    `remember(json)`, `index_incident(json)` and `list_incidents()`.

#### 6. `src/remediation.py` (Create)

*   **Type / Scope**: Python Workflow
*   **Proposed Changes**: `RemediationWorkflow.run` as described in §7.

#### 7. `src/sim.py`, `src/tools.py`, `src/guardrails.py`, `src/memory.py`, `src/copilot.py`, `src/scripted.py`, `src/prompts.py`, `src/db.py` (Create)

*   **Type / Scope**: Pure Python modules. None imports `workers`, `js` or
    `pyodide`, except `db.py`, which wraps cursor conversion.
*   **Proposed Changes**: as described in §3 to §6.

#### 8. `tests/test_sim.py`, `tests/test_guardrails.py`, `tests/test_memory.py`, `tests/test_copilot.py` (Create)

*   **Type / Scope**: `unittest`
*   **Proposed Changes**: fault tables and decay, deterministic values;
    validator cases (string ints, bool rejection, enums, ranges, unknown
    keys); number check cases from CUJ 6; similarity threshold and
    ranking; `Copilot` loop with `ScriptedModel`; a malformed-call model
    (one retry, then stop); a model that writes its tool call in
    `response`; and the rewrite path.

#### 9. `web/` (Create)

*   **Type / Scope**: Next.js app (`create-next-app`, TypeScript, Tailwind)
*   **Proposed Changes**: `next.config.ts` (`output: "export"`),
    `app/page.tsx` (the UI in §10), `lib/api.ts` (fetch wrappers;
    `NEXT_PUBLIC_API_BASE` defaults to `""`), and components
    `ServicePanel`, `Chat`, `ProposalCard` and `AuditLog`.

#### 10. `package.json` (Create, root)

*   **Type / Scope**: scripts. `build` (`npm --prefix web run build`), `dev`
    (build, then `wrangler dev`), `deploy` (build, then `wrangler deploy`),
    and `test` (`python3 -m unittest discover -s tests`). `wrangler` is a
    pinned dev dependency.

#### 11. `README.md`, `PROMPTS.md` (Create)

*   **Type / Scope**: Docs. The README has the live URL, a summary, the
    architecture diagram, local and deploy steps, the demo walkthrough
    (CUJ 1 to 4), and the guardrails and trade-offs.

### Verification Plan

*   **V1 Build**: `npm run build` produces `web/out/index.html`.
*   **V2 Unit tests**: `npm test`. All pass on CPython 3.13.
*   **V3 Local e2e (scripted)**: `wrangler dev` with `LLM_MODE=scripted`,
    then run CUJ 1 to 5 with `curl`. Check the proposal and Workflow
    statuses, that verify retries during the decay, the memory match on the
    second incident, and the rejection path.
*   **V4 Lint**: `python3 -m py_compile src/*.py`, and `next build` type
    check.
*   **V5 Live**: after `wrangler login` and deploy, run CUJ 1 and 4 against
    Llama 3.3. Confirm the Q1 tool-message format, and record the results in
    the README.
*   **V6 Live reliability**: run CUJ 1 and CUJ 4 ten times each against the
    deployed app. Report the pass rate: correct version named, number check
    passed, and the right fix proposed.

--------------------------------------------------------------------------------

<!-- Provenance: Indicates initial structural drafting via EGM. -->

*Drafted with EGM*
