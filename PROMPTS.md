# Prompt history

AI-assisted development log for `cf_ai_oncall_copilot`. Prompts are recorded in
the order they were given, starting from the first one. Tool: Claude Code
(Claude Opus 5.5). Process: Elephant-Goldfish Model (spec first, fresh-context
review, then build to the spec).

---

## 1. Kickoff: execute the plan with EGM

> I have created this plan to built a cloudflare agentic workflow for incident
> response flows. Help me execute this plan. Use this framework:
> https://github.com/google/elephant-goldfish-model . This is the plan:
>
> *(Full plan pasted: "Cloudflare AI App Plan: cf_ai_oncall_copilot". It is
> kept verbatim as the concept brief in [`concepts_brief.md`](concepts_brief.md).)*

What happened: read the EGM repo (skills, design-doc template, reviewer
personas) and scaffolded the official Cloudflare `agents-starter` to check the
current SDK.

## 2. Stack change

> Use python to create this and UI can be in next.js

What happened: dropped the TypeScript Agents SDK starter. The backend is now a
Python Worker (Durable Objects, a Python Workflow, and Workers AI through the
binding). The UI is a Next.js static export served from the same Worker's
static assets. Python SDK behavior (`step.wait_for_event`, `send_event`,
Durable Object stubs) was confirmed from the `workers-runtime-sdk` 1.9.6 source.

## 3. Design review prompts (goldfish reviewers)

The design doc was reviewed by fresh-context subagents that saw only the doc
and the EGM persona files. Their prompts are recorded in
[`docs/designs/review/`](docs/designs/review/).

## 4. Runtime prompts

The system prompt and tool descriptions sent to Llama 3.3 live in
[`src/prompts.py`](src/prompts.py), so the history of the model's instructions
can be read in git.

## 5. Running locally: URL passed as an argument

> *(Pasted terminal output: `npm run dev http://localhost:8787` failing with
> `The entry-point file at "http:/localhost:8787" was not found`.)*
>
> Why am I getting this error? Should I put it under quotes?

What happened: the `# http://localhost:8787` in the README is a shell comment
that shows where the server listens. Typed without the `#`, npm appended it to
`pywrangler dev`, and wrangler read it as the entry-point path. Quoting
wouldn't help. The fix is to run `npm run dev` with no arguments.

## 6. Remote AI session needs a workers.dev subdomain

> *(Pasted terminal output: `npm run dev` failing with "You need to register a
> workers.dev subdomain before running the dev command in remote mode".)*
>
> How do u do this?

What happened: the Workers AI binding has no local emulation, so `wrangler dev`
opens a remote session for it, and Cloudflare only allows that once the
account has a `workers.dev` subdomain. Two options: run `npm run dev:scripted`
(no AI binding, fixed planner, no login), or register the subdomain in the
dashboard and rerun `npm run dev`.

## 7. Publish to GitHub

> Push this to git so I can connect

What happened: checked `.gitignore` and searched the files for secrets (none
found), made the initial commit, and created the public repo
`bandhanp1804/cf_ai_oncall_copilot` with `gh repo create --push`. Workers Builds
settings were noted: build command `npm run build`, and a Python-aware deploy
command if the default `npx wrangler deploy` fails.

## 8. "The model is unavailable right now"

> *(Screenshot: the copilot replying "The model is unavailable right now; no
> actions were taken.")*

What happened: that text is the copilot's fallback when the model call fails
(`Copilot._model` in `src/copilot.py`, which also writes a `model_error` audit
entry). The cause was the same missing subdomain: the remote AI session never
started, so every `env.AI.run` call threw. A test incident against the scripted
server got a full answer, which confirmed the rest of the app worked.

## 9. Subdomain registered

> I did register this subdomain:
>
> *(Screenshot: Worker URL settings showing the `workers.dev` production and
> preview URLs, with the production toggle off.)*

What happened: started a second `npm run dev` on port 8790 and sent a test
chat. It came back in `workers-ai` mode with 5 tool calls and no `model_error`.
Two follow-ups were flagged. First, with no fault injected, Llama called a
baseline 0.6% error rate "high" and proposed a rollback (the approval gate
stopped it). Second, the production URL toggle was off.

## 10. Duplicate user message on Enter

> Enter is doing double prompting so fix this
>
> *(Screenshot: the same user message shown twice while "Investigating…" is
> displayed.)*

What happened: only one request reached the server. The agent saves the user
message before it calls the model, and it rejects a second chat while busy.
The UI's mid-turn poll returned that saved message while the optimistic bubble
was still showing. Fix in `web/app/page.tsx`: the optimistic bubble records the
newest message id at send time and hides once a newer user message arrives.
The match is on id, so repeating the same question still works.

## 11. Commit the fix

> Yes

What happened: committed the `web/app/page.tsx` fix and pushed it to
`origin/main`.

## 12. Update this log

> Also add all the prompt history to PROMPTS.md along with a summarized reply.
> The instructions are in AGENTS.md

What happened: added entries 5 to 12, in the format of the entries above. The
only AGENTS.md in the repo (`web/AGENTS.md`) is the Next.js notice and has no
PROMPTS.md instructions. Terminal logs are summarized rather than pasted, and
the Cloudflare account ID is left out.

## 13. Check the repo

> https://github.com/bandhanp1804/cf_ai_oncall_copilot

What happened: confirmed the repo is public with all three commits on `main`.
The Workers Builds check on the latest commit had failed. A local
`pywrangler deploy --dry-run` passed, which pointed to the build settings
rather than the code. Likely causes: `web/` dependencies not installed, no
build command, or a deploy command that doesn't run `pywrangler`. Suggested
settings: build `npm --prefix web ci && npm run build`, deploy
`pip install workers-py==1.17.9 && pywrangler deploy`.

## 14. Deploy and record a demo video

> Is it possible to create a demo video and deploy it using the live cf domain?
> Also keep updating PROMPTS.md with the prompts

What happened: with the user's go-ahead, deployed with `npm run deploy` to
`cf-ai-oncall-copilot.risingspy007-fe3.workers.dev`, which also enabled the
`workers.dev` URL. Recorded the demo against the live site with a headless
Playwright script: trigger Bad deploy, ask "Checkout is throwing 500s since
the last deploy. What's going on?", then approve the proposed rollback. Llama
3.3 made 4 tool calls, quoted the 24.5% error rate against a 0.6% baseline,
and proposed rolling back v2.15.0 → v2.14.1. After approval the Workflow
verified the error rate at 0.4% and resolved the incident. The 57 s video is
saved as `docs/demo.mp4` (0.9 MB) and linked, with the live URL, at the top of
the README. This log is now updated after every prompt.

## 15. Fix the Workers Builds failure

> Commit it to git. Also build is failing on cf.
>
> *(Pasted Workers Builds log ending in `sh: 1: next: not found` during
> `npm run build`.)*

What happened: Workers Builds runs `npm clean-install` only at the repo root,
so `web/`'s dependencies (including `next`) were never installed. Added a root
`postinstall` (`npm --prefix web ci`). Plain `wrangler deploy` would fail next,
because `pywrangler` bundles the Python Workers SDK at deploy time, so added a
`deploy:ci` script that installs `workers-py` and runs `pywrangler deploy`.
Checked by replaying the CI steps in a fresh clone: `npm ci`, `npm run build`,
and `deploy:ci` as a dry run all passed. Documented the Workers Builds settings
in the README. The earlier demo-video work was already committed and pushed.

## 16. Push

> Push the latest changes to git

What happened: `main` already matched `origin/main`, with no uncommitted
changes. The only new change was this log entry, which was committed and
pushed.

## 17. Healthy-service rule in the system prompt

> add a rule to src/prompts.py: "If ratio_to_baseline is near 1 and there are
> no ERROR logs, say checkout looks healthy and do not propose a fix."

What happened: added the rule, word for word, to the Rules list in `SYSTEM`.
The 36 unit tests pass. Tested against real Llama 3.3 with `npm run dev` on a
spare port. With no fault, "Why is checkout failing?" still got a rollback
proposal 3 times out of 3, the model calling a 0.6% error rate (ratio 1.0)
"high". With a bad deploy it still correctly proposed rolling back to v2.14.1.
So the rule on its own does not change the model's behavior. Options offered:
move the check into the step-2 instructions, or add a code guardrail that
rejects `propose_remediation` when the evidence shows a healthy service.

## 18. Script for the live Llama check

> Write a script to run a live Lllama check

What happened: added `scripts/llama_check.py` (standard library only) and
`npm run check:llama`. For each scenario (no fault, `bad_deploy`,
`db_pool_exhaustion`, `latency_spike`) it opens a fresh sandbox, sets the
fault, waits for it to take effect, asks "Why is checkout failing?", and checks
the proposal against the fix the simulator expects (none when healthy). It
exits if the server is in scripted mode, counts `model_error` entries as
failures, and exits 1 on any failure. A first run with one run per scenario:
3/4 passed. All three faults got the right fix, and the healthy case still got
a rollback proposal (the problem from entry 17). Documented in the README.

## 19. Llama check refused a scripted server

> *(Pasted terminal output: `npm run check:llama` stopping with "server is in
> 'scripted' mode, not workers-ai; run `npm run dev`, not dev:scripted".)*

What happened: the script's mode guard worked as intended. The server on port
8787 was still the earlier `npm run dev:scripted` one, which has no AI binding,
so the check would have tested the fixed planner instead of Llama. Fix: stop
that server and start `npm run dev`, or point the check at the deployed Worker
with `--base`.

## 20. Dev server came up on port 8788

> *(Pasted terminal output: `npm run dev` starting with the remote AI binding,
> ending in "Ready on http://localhost:8788".)*

What happened: the real-AI server started on 8788 because the old scripted
server was still holding 8787, which is why the check kept seeing scripted
mode. Fix: run the check with `--base http://localhost:8788`, or stop the old
server and restart `npm run dev` so it takes 8787.

## 21. Code guardrail for the healthy-service false positive

> Looks like the false positive didn't work
>
> *(Pasted `npm run check:llama` output: 9/12. `none` proposed `rollback_deploy`
> 3/3; all three faults passed 3/3.)*

What happened: added the code guardrail from entry 17.
`guardrails.shows_problem(sources)` is true only when a tool result shows a
symptom: a metric with `ratio_to_baseline` ≥ 1.5, or ERROR/WARN log lines.
`Copilot._run_tool` now rejects `propose_remediation` without that evidence.
The model gets a tool error telling it to check the other metrics and logs,
and to say checkout looks healthy if they are normal. The block is recorded in
the audit log. Added 3 unit tests (39 pass). Live check against real Llama:
`none` 5/5 with no proposal (each answer "Checkout looks healthy…"), and
`bad_deploy`, `db_pool_exhaustion` and `latency_spike` 2/2 each with the right
fix.

## 22. Push

> Push to git

What happened: committed and pushed the healthy-service work: the prompt rule,
the `shows_problem` guardrail and its tests, `scripts/llama_check.py`, the
README updates, and PROMPTS.md entries 17 to 22. The deployed Worker was not
redeployed.

## 23. Llama check against the live Worker

> *(Pasted traceback: `npm run check:llama -- --base https://…workers.dev`
> crashing with `JSONDecodeError` on `POST /api/incidents`.)*

What happened: Cloudflare answered `403 error code: 1010`, which blocks
urllib's default `Python-urllib` User-Agent, and the script crashed parsing
that plain-text body as JSON. The script now sends its own User-Agent, exits
with the status and body on non-JSON errors, and prints the `model_error` text
when a run fails. The next live run hit `AiError 4006`: the account had used
its daily free Workers AI allocation (10,000 neurons), mostly on the live
checks and demo recording. Until the allocation resets at 00:00 UTC, every chat
on the live site gets the "model is unavailable" fallback. Options given: wait
for the reset, move to the Workers Paid plan, or demo with
`npm run dev:scripted`.

## 24. Push the script fix

> Yes do that

What happened: committed and pushed the `llama_check.py` fixes (User-Agent,
readable HTTP errors, `model_error` text) and PROMPTS.md entries 23 and 24.

## 25. Quota fallback and a fallback URL

> Also have a fallback url

What happened: two changes. First, an automatic fallback in
`IncidentAgent._turn`: when a Workers AI call fails with `4006` (daily free
allocation used up), the turn switches to `ScriptedModel` from that call on,
records `model_fallback` in the audit log, and tags the reply
`llm_mode: "scripted-fallback"`. The UI then shows an amber badge and a banner
with the fallback link (`FALLBACK_URL` var, returned as `fallback_url`).
Second, a fallback URL: `npm run deploy:scripted` deploys the `scripted` env as
its own Worker, `cf-ai-oncall-copilot-scripted.risingspy007-fe3.workers.dev`,
which has no AI binding. Deployed both Workers; the first scripted deploy hit a
network error and the retry succeeded. With the quota still used up, a bad
deploy on the main site was answered in `scripted-fallback` mode with the right
rollback, and the fallback URL answered in `scripted` mode. `check:llama` now
reads each reply's mode, so a fallback answer is never counted as a Llama pass.
It exits with a quota message instead. README updated with both URLs.

## 26. Push

> Push the code

What happened: committed and pushed the quota fallback, the fallback Worker
setup, the `check:llama` mode fix, the README updates, and PROMPTS.md entries
25 and 26. Both Workers were already deployed with this code.

## 27. Design doc and architecture in ASD-STE100

> Also update the design docs and the architecture to use asd-ste100 standard

What happened: rewrote `docs/designs/oncall_copilot.md` and the README
Architecture and Guardrails sections in ASD-STE100 (Simplified Technical
English). That means short sentences (25 words or fewer for descriptions, 20
for instructions), one idea per sentence, active voice, simple tenses, and
instructions as commands. Code names stay as they are. A script checked
sentence length, and the 2 sentences over the limit were split. The rewrite
also brought the content up to date: new goals G7 (no fix for a healthy
service) and G8 (quota fallback), CUJ 7 and CUJ 8, the evidence gate (§5),
the three model modes and the quota fallback (§6), and a new Deployment
section (§11: two Workers, Workers Builds settings). It also updated the
`wrangler.jsonc`, `package.json`, RPC method, test count and V6 live-check
results to match the code. The README diagram now shows the fallback
planner. The goldfish review report was not changed, because it records what
the reviewers wrote.

## 28. Cloudflare Pages for the static page

> Also see if we can integrate the pages api to get have the single static page

What happened: answered as a question, with no code changed. The UI is
already one static page: the Worker serves the Next.js export (`web/out`)
through Workers static assets, on the same origin as `/api/*`. Moving it to
Pages is possible (`wrangler pages deploy web/out`, `NEXT_PUBLIC_API_BASE`
set to the Worker URL, and the Pages origin added to the CORS allow-list in
`src/entry.py`). It would add a second deploy and cross-origin requests, which
is why the design doc rejected it (Alternative D). The user was asked what
they want Pages for before building it.

## 29. Record the Pages decision

> Okay so add this decision to the readme on why we rejected pages

What happened: added "Workers static assets instead of Cloudflare Pages" to
the README Trade-offs section, in ASD-STE100 style. The UI is one static page
that the Worker serves on the same origin as `/api/*`. Pages would add a
second deploy (so the UI and API can be on different versions) and
cross-origin requests that need CORS for `pages.dev`. Cloudflare also
recommends Workers static assets for new projects. It links to Alternative D
in the design doc.

## 30. Push

> Yes

What happened: committed and pushed the ASD-STE100 design doc, the README
Architecture, Guardrails and Pages-decision changes, and PROMPTS.md entries 27
to 30.

## 31. Fix the user's live_check.py

> *(Pasted output: `python3 scripts/live_check.py --base https://…workers.dev`,
> with all 10 runs failing CUJ 1 and CUJ 4 on `/api/service/reset -> HTTP 403:
> error code: 1010`, and a "README row" of `CUJ 1: 0/10, CUJ 4: 0/10`.)*
>
> I added another script for live check so look into that

What happened: reviewed the user's `scripts/live_check.py` (CUJ 1 and CUJ 4,
10 runs each). Four fixes. (1) It sends its own User-Agent, because Cloudflare
blocks urllib's default with 403 error 1010. (2) Setup and HTTP errors raise
`Abort` and stop the run with exit code 2, instead of scoring them as model
failures. Before this, the script printed a false "0/10" README row. (3) It
stops if an answer is not from Llama (`scripted` mode, or
`scripted-fallback` when the quota is used up), so it does not use quota on
runs that cannot pass. (4) `live_check_results.json` is now gitignored. Added
`npm run check:live` and a README note. Tested on the live Worker: requests now
get through, and the script stopped with the quota message. A scored run must
wait for the 00:00 UTC quota reset.

## 32. Push

> Push it

What happened: committed and pushed `scripts/live_check.py` with the fixes,
`npm run check:live`, the README note, the `.gitignore` entry, and PROMPTS.md
entries 31 and 32.
