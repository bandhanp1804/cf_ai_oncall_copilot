"""One chat turn: the tool loop, tool-call parsing, and the number check.

Pure Python with injected async callables, so it runs under CPython tests
exactly as it runs inside the IncidentAgent Durable Object.
"""

import json

import guardrails
import prompts
from tools import READ_ONLY, TOOLS_BY_NAME, model_tools

MAX_MODEL_STEPS = 6
MAX_CALLS_PER_STEP = 4
MAX_RESULT_CHARS = 4000
SCHEMA_FAILURE_CAP = 2
MODEL_UNAVAILABLE = "The model is unavailable right now; no actions were taken."


def _loads(value):
    if isinstance(value, str):
        try:
            return json.loads(value) if value.strip() else {}
        except ValueError:
            return value
    return value


def _text_tool_calls(text):
    """Tool calls the model wrote as text instead of using tool_calls.

    Accepted only when the whole response is one JSON object (or a list of
    them) naming a known tool, so prose that happens to contain JSON is never
    executed.
    """
    stripped = text.strip()
    if stripped.startswith("<|python_tag|>"):
        stripped = stripped[len("<|python_tag|>") :].strip()
    if not stripped or stripped[0] not in "{[":
        return []
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    calls = []
    for item in items:
        if not isinstance(item, dict):
            return []
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if name not in TOOLS_BY_NAME:
            return []
        calls.append({"id": "", "name": name, "arguments": fn.get("parameters", fn.get("arguments", {}))})
    return calls


def parse_model_response(resp):
    """Return (tool_calls, text) from a Workers AI response in any of its shapes."""
    if not isinstance(resp, dict):
        return [], str(resp or "")
    message = resp
    if resp.get("choices"):
        message = resp["choices"][0].get("message", {}) or {}
    text = message.get("response", message.get("content", "")) or ""
    raw_calls = message.get("tool_calls") or []
    calls = []
    for c in raw_calls:
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        calls.append({"id": c.get("id", "") or "", "name": fn.get("name", ""), "arguments": fn.get("arguments", {})})
    if isinstance(text, (dict, list)):
        # Workers AI sometimes returns an already-parsed JSON body here.
        text = json.dumps(text)
    if not calls:
        calls = _text_tool_calls(text)
        if calls:
            text = ""
    return calls, text


class Copilot:
    def __init__(self, run_model, execute_tool, audit):
        """
        run_model(messages, tools) -> dict    async; tools is None for no tools
        execute_tool(name, args) -> dict      async; read-only tools or propose_remediation
        audit(kind, detail)                   sync
        """
        self.run_model = run_model
        self.execute_tool = execute_tool
        self.audit = audit

    async def _model(self, messages, tools):
        try:
            return await self.run_model(messages, tools)
        except Exception as exc:  # AI errors, quota, timeouts
            self.audit("model_error", {"error": str(exc)[:500]})
            return None

    async def run_turn(self, history, user_text, evidence, prior_findings=0):
        """
        history: [{"role": "user"|"assistant", "content": str}] before this turn
        evidence: tool results and user messages already in this incident
        prior_findings: read-only findings already stored for this incident
        Returns {"text", "number_check", "proposal_id", "tool_calls"}.
        """
        messages = [{"role": "system", "content": prompts.SYSTEM}]
        messages += history[-12:]
        messages.append({"role": "user", "content": user_text})
        sources = list(evidence) + [user_text]
        findings = prior_findings
        schema_failures = {}
        proposal_id = ""
        n_calls = 0
        draft = None
        stopped = False

        for _ in range(MAX_MODEL_STEPS):
            resp = await self._model(messages, model_tools())
            if resp is None:
                return self._result(MODEL_UNAVAILABLE, {"ok": True, "unverified": []}, proposal_id, n_calls)
            calls, text = parse_model_response(resp)
            if not calls:
                draft = text
                break

            results = []
            for i, call in enumerate(calls):
                name = call["name"]
                if i >= MAX_CALLS_PER_STEP:
                    results.append((call, {"error": "too many tool calls in one step; call at most 4 at a time"}))
                    continue
                result = await self._run_tool(call, schema_failures, findings, sources)
                if name in READ_ONLY and "error" not in result:
                    findings += 1
                    sources.append(result)
                if name == "propose_remediation" and result.get("proposal_id"):
                    proposal_id = result["proposal_id"]
                    sources.append(result)
                results.append((call, result))
                n_calls += 1
                if schema_failures.get(name, 0) >= SCHEMA_FAILURE_CAP:
                    self.audit("tool_error", {"tool": name, "error": "argument validation failed twice; stopping tool loop"})
                    stopped = True
            messages += self._tool_messages(results)
            if stopped:
                break

        if draft is None or not draft.strip():
            # Loop exhausted or stopped: one answer from the evidence, no tools.
            messages.append({"role": "user", "content": prompts.FORCE_ANSWER})
            resp = await self._model(messages, None)
            if resp is None:
                return self._result(MODEL_UNAVAILABLE, {"ok": True, "unverified": []}, proposal_id, n_calls)
            _, draft = parse_model_response(resp)
            draft = draft or "I could not produce an answer from the evidence."

        text, check = await self._number_check(messages, draft, sources)
        return self._result(text, check, proposal_id, n_calls)

    async def _run_tool(self, call, schema_failures, findings, sources):
        name = call["name"]
        args = _loads(call["arguments"])
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            schema_failures[name] = schema_failures.get(name, 0) + 1
            self.audit("tool_error", {"tool": name, "error": "unknown tool"})
            return {"error": f"unknown tool '{name}'", "available": sorted(TOOLS_BY_NAME)}
        try:
            args = guardrails.validate_args(spec["parameters"], args)
        except guardrails.ToolArgError as exc:
            schema_failures[name] = schema_failures.get(name, 0) + 1
            self.audit("tool_error", {"tool": name, "args": args, "error": str(exc)})
            return {"error": str(exc), "expected": spec["parameters"]}
        if name == "propose_remediation" and findings == 0:
            return {"error": "investigate first: call get_metrics, get_recent_deploys or search_logs before proposing a fix"}
        if name == "propose_remediation" and not guardrails.shows_problem(sources):
            self.audit("tool_error", {"tool": name, "args": args, "error": "blocked: no evidence of a problem"})
            return {"error": guardrails.NO_PROBLEM}
        try:
            result = await self.execute_tool(name, args)
        except Exception as exc:
            self.audit("tool_error", {"tool": name, "args": args, "error": str(exc)[:500]})
            return {"error": f"tool failed: {str(exc)[:200]}"}
        self.audit("tool_call", {"tool": name, "args": args, "ok": "error" not in result})
        return result

    @staticmethod
    def _tool_messages(results):
        """The assistant's calls followed by one tool message per result."""
        calls = [{"name": c["name"], "arguments": _loads(c["arguments"])} for c, _ in results]
        out = [{"role": "assistant", "content": json.dumps(calls if len(calls) > 1 else calls[0])}]
        for call, result in results:
            msg = {"role": "tool", "name": call["name"], "content": json.dumps(result)[:MAX_RESULT_CHARS]}
            if call.get("id"):
                msg["tool_call_id"] = call["id"]
            out.append(msg)
        return out

    async def _number_check(self, messages, draft, sources):
        check = guardrails.check_numbers(draft, sources)
        if check["ok"]:
            self.audit("number_check", {"ok": True})
            return draft, {**check, "rewritten": False, "redacted": False}

        self.audit("number_check", {"ok": False, "unverified": check["unverified"], "draft": draft})
        retry = messages + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": prompts.REWRITE.format(numbers=", ".join(check["unverified"]))},
        ]
        resp = await self._model(retry, None)
        rewritten = parse_model_response(resp)[1] if resp is not None else ""
        if rewritten.strip():
            second = guardrails.check_numbers(rewritten, sources)
            if second["ok"]:
                self.audit("number_check", {"ok": True, "after_rewrite": True})
                return rewritten, {"ok": True, "unverified": [], "rewritten": True, "redacted": False}
            text, unverified = rewritten, second["unverified"]
        else:
            text, unverified = draft, check["unverified"]
        self.audit("number_check", {"ok": False, "unverified": unverified, "redacted": True, "draft": text})
        return (
            guardrails.redact_numbers(text, unverified),
            {"ok": False, "unverified": unverified, "rewritten": True, "redacted": True},
        )

    @staticmethod
    def _result(text, check, proposal_id, n_calls):
        return {"text": text, "number_check": check, "proposal_id": proposal_id, "tool_calls": n_calls}
