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
