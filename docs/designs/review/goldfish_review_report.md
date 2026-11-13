# Goldfish review: oncall_copilot.md

* **Date:** 2026-11-12
* **Reviewers:** two fresh-context subagents (no conversation history). Each
  got only the design doc, the concept brief, and one EGM persona:
  `agents/design_critic.md` (DC) and `agents/implementation_readiness.md`
  (IR). IR also had the `workers-runtime-sdk` 1.9.6 source, so it could check
  the SDK claims.
* **Result:** 24 findings. 21 accepted, 2 partly accepted, 1 contested.
  Accepted changes are folded into the design doc in "Review resolutions".

## Reviewer prompts

**DC:** "You are an EGM goldfish reviewer with no prior context. Adopt the
persona in `agents/design_critic.md`, and skim
`document_standards/feature-spec-standard.md`. Review `oncall_copilot.md`.
Focus on gaps between goals, CUJs and the plan; guardrail holes; race
conditions; Llama 3.3 tool-calling failure modes; and anything a reviewer of
a Cloudflare internship assignment would flag. Give at most 12 findings,
ranked, each with a concrete fix."

**IR:** "You are an EGM goldfish reviewer with no prior context. Adopt the
persona in `agents/implementation_readiness.md`. Could an engineer implement
this without asking questions? Check every assumption about `step.do`,
`wait_for_event`, `send_event`, Durable Object stubs and RPC conversion
against the real SDK source. Give at most 12 findings, ranked, each citing
the SDK file and line."

## Findings and resolutions

| # | Sev | Finding (condensed) | Resolution |
|---|---|---|---|
| IR1 | blocker | `step.do` signature depends on the `python_workflows_implicit_dependencies` flag; step functions with parameters raise | **Accept.** Step functions take no parameters and read values from closures. This works in both modes. `compatibility_date` pinned to `2026-11-01`. |
| IR2 / DC6 | blocker | `run()` replays from the top, so reports outside `step.do` repeat; `remember` INSERT fails on retry | **Accept.** Every report gets its own uniquely named `step.do`. `workflow_update` dedupes on `(proposal_id, step)`. `remember` uses `INSERT OR REPLACE`. |
| DC1 | blocker | Approval can arrive after the state changed (Reset, another fault, another incident's fix); `apply` acts on stale state | **Accept.** `apply_remediation` is a compare-and-set on `expected_version` and `expected_fault` from the snapshot. A mismatch raises `NonRetryableError`, and the proposal ends `stale`. |
| DC2 | blocker | Number check can be bypassed through tool args, model-written memory text, and timestamps | **Partly accept.** The allowed set is built only from tool result values. Args, timestamp fields, ids and free-text fields written by the model (`root_cause`, `title`, `reason`) are excluded. Numbers inside log `msg` strings stay allowed. The scope stays incident-wide rather than current-turn, because follow-up questions must be able to quote earlier evidence. Matching each number to its metric is **contested** as brittle for prose and listed under Future Improvements. |
| DC3 | major | False failures: p95, times, "100% of pool", ratios, ms→s | **Accept.** The skip list covers p50/p95/p99, times, dates and HTTP codes. The tools return derived values, so the model can quote them: `ratio_to_baseline`, `pool_utilization_pct`, and seconds for `_ms` values. Precision means decimal places only. Cases are added to the tests. |
| DC4 | major | Showing an unverified answer with a badge contradicts "unacceptable" | **Accept.** After a failed rewrite, unverified numbers are replaced with `[unverified]`. The raw draft goes only to the audit log. |
| DC5 | major | `decide` race (double click; approve and reject at once); a failed `create` leaves `pending` forever | **Accept.** `decide` flips the status synchronously before any await. The row is deleted if `create` raises. `get_state` marks proposals older than 65 min as `expired`. |
| IR3 | major | "Report failed with last snapshot" is impossible: an exhausted step re-raises with only the message | **Accept.** `await verify()` is wrapped in `try/except Exception`, then a `final_snapshot` step runs before `report_failed`. |
| IR4 | major | `apply` retries are not idempotent | **Accept.** `apply_remediation` is keyed by `proposal_id` and returns the stored result when the id was already applied. Validation errors raise `NonRetryableError`. |
| IR5 | major | `REMEDIATION.create()` call shape not specified | **Accept.** `create({"id": proposal_id, "params": {...}})`, `workflow_id = proposal_id`, and `run` reads `event["payload"]`. |
| IR6 | major | Proposal and incident state machines missing | **Accept.** A transition table was added (Review resolutions R4). |
| IR7 | major | Is `Copilot` sync or async? | **Accept.** `async def run_turn` with async callables; tests use `IsolatedAsyncioTestCase`. `busy` is an in-memory flag set and cleared in `try/finally`. If the object is evicted, the flag resets, which is safe because the turn died with it. |
| IR8 | major | `db.py` cursor conversion undefined; the reason for avoiding None is wrong (the SDK maps None to `jsnull`) | **Accept.** `db.py` defines `rows`/`one`/`run` and converts both cursor and row shapes. The no-None rule is kept only as a convention. |
| IR9 | minor | Stub retrieval not named | **Accept.** `env.X.getByName(name)`. All Durable Object RPC uses JSON strings in both directions. |
| IR10 | minor | Step config literals; wait timeout raises a raw JS error; CUJ 5 takes about 2.5 min | **Accept.** Literal configs are given. Verify uses `limit 4, delay "3 seconds", exponential`, so CUJ 5 fails in about 45 s. Any exception from `wait_for_event` counts as expired. |
| IR11 | minor | Approval may arrive before the Workflow reaches `wait_for_event` | **Accept.** The proposal card enables Approve only after the `report_awaiting` step sets `awaiting_approval`, and `decide` refuses any other status. |
| IR12 | minor | wrangler config incomplete; tuples; unreachable ASSETS fallback | **Accept.** The full literal config is in the doc, nothing passed to the SDK uses tuples, and the fallback is removed. |
| DC7 | major | A singleton service on a public URL is shared by every visitor; no rate limit | **Accept.** The `X-Sandbox-Id` header (a random id kept in `localStorage`) keys `CheckoutService` as `checkout-<sid>`. Incidents and memory are scoped to that sandbox. A Workers Rate Limiting binding allows 10 chat turns per 60 s per sandbox. |
| DC8 | major | Llama failure modes beyond schema errors | **Accept.** Model and tool exceptions are caught and audited as `model_error` / `tool_error`. The JSON-in-text fallback applies only when the whole trimmed response (after removing `<\|python_tag\|>`) is one object naming a known tool. Only schema failures count toward the retry cap. `propose_remediation` needs at least one read-only finding first. At most 4 calls per response are executed. |
| DC9 | major | Re-triggering `bad_deploy` collides on the PK; "older" is ambiguous; thresholds conflict | **Accept.** The deploy row is upserted with a fresh `at`. "Older" means an earlier `at`. The single threshold is 0.5. `recent_deploy` means the *current* version was deployed within 30 min. A test computes the expected score. |
| DC10 | major | G2 is false as written (demo controls change state) and only enforced by convention | **Accept.** G2 now excludes the demo controls. `apply_remediation` calls `IncidentAgent.proposal_status` and requires `approving`. A unit test checks that tool dispatch cannot reach it. |
| DC11 | major | Per-minute buckets vs a 20 s decay make verify nondeterministic | **Accept with clarification.** `current` and verify use the exact `t`; only jitter and history points are per minute. Expected verify: fails at about 0, 3 and 9 s, and passes at about 21 s. |
| DC12 | minor | No Risks section; LLM goals unmeasured; the scripted model looks like a scripted demo; ordering of Workflow messages during a turn | **Accept.** A Risks section was added. V6 runs CUJ 1 and CUJ 4 live ten times and records pass rates. The scripted mode is local-only, shows a badge, and defaults off. Alternatives now carry technical trade-offs. The rewrite runs with tools disabled. Approve sets `mitigating`. Workflow messages that arrive during a turn are queued and flushed after it. |
