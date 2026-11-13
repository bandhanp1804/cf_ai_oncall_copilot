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
