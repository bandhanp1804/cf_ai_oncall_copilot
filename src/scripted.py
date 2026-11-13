"""A fixed planner that stands in for Llama 3.3.

Used as the Copilot test double, and for local runs without a Cloudflare
login (LLM_MODE=scripted in .dev.vars). It speaks the same response shape as
Workers AI and writes its answer only from tool results, so it exercises the
real tool loop, validation and number check.
"""

import json

import prompts

CONTROL_PROMPTS = (prompts.REWRITE.split("{")[0], prompts.FORCE_ANSWER[:40])


def _turn_results(messages):
    """Tool results since the user's last real message, keyed by tool (and metric)."""
    start = 0
    for i, m in enumerate(messages):
        if m["role"] == "user" and not m["content"].startswith(CONTROL_PROMPTS):
            start = i
    calls, results = [], {}
    for m in messages[start:]:
        if m["role"] == "assistant" and m["content"].startswith(("{", "[")):
            parsed = json.loads(m["content"])
            calls += parsed if isinstance(parsed, list) else [parsed]
        elif m["role"] == "tool":
            call = calls.pop(0) if calls else {"name": m.get("name"), "arguments": {}}
            key = call["name"]
            if key == "get_metrics":
                key += ":" + call["arguments"].get("metric", "")
            results[key] = json.loads(m["content"])
    return results


def _call(name, **arguments):
    return {"name": name, "arguments": arguments}


def diagnose(results):
    """(fault, evidence line) from the tool results."""
    logs = results.get("search_logs", {}).get("lines", [])
    text = " ".join(line["msg"] for line in logs)
    pool = results.get("get_metrics:db_pool_in_use", {})
    if "PaymentClientV2" in text:
        return "bad_deploy", next(line["msg"] for line in logs if "PaymentClientV2" in line["msg"])
    if "DB connection" in text or pool.get("pool_utilization_pct", 0) >= 95:
        line = next((line["msg"] for line in logs if "DB connection" in line["msg"]), "")
        return "db_pool_exhaustion", line
    if "payments-gateway" in text:
        return "latency_spike", next(line["msg"] for line in logs if "payments-gateway" in line["msg"])
    # No log signature yet (a fault that just started): fall back to telemetry.
    tags = results.get("find_similar_incidents", {}).get("fingerprint", [])
    if "high_error_rate" in tags and "recent_deploy" in tags:
        return "bad_deploy", ""
    if "pool_saturated" in tags:
        return "db_pool_exhaustion", ""
    if "high_latency" in tags:
        return "latency_spike", ""
    if "high_error_rate" in tags:
        return "unknown", ""
    return "none", ""


class ScriptedModel:
    async def __call__(self, messages, tools):
        results = _turn_results(messages)
        if tools is not None:
            if "get_metrics:error_rate_pct" not in results:
                return {
                    "response": None,
                    "tool_calls": [
                        _call("get_metrics", metric="error_rate_pct", window_minutes=15),
                        _call("get_metrics", metric="p95_latency_ms", window_minutes=15),
                        _call("get_metrics", metric="db_pool_in_use", window_minutes=15),
                        _call("get_recent_deploys", limit=5),
                    ],
                }
            if "search_logs" not in results:
                return {
                    "response": None,
                    "tool_calls": [
                        _call("search_logs", level="ERROR", limit=10),
                        _call("find_similar_incidents", query="checkout errors"),
                    ],
                }
            fault, _ = diagnose(results)
            if fault not in ("none", "unknown") and "propose_remediation" not in results:
                return {"response": None, "tool_calls": [self._proposal(fault, results)]}
        return {"response": self._answer(results)}

    @staticmethod
    def _proposal(fault, results):
        if fault == "bad_deploy":
            deploys = results["get_recent_deploys"]["deploys"]
            target = deploys[1]["version"] if len(deploys) > 1 else deploys[0]["version"]
            return _call(
                "propose_remediation",
                action="rollback_deploy",
                target_version=target,
                reason=f"{deploys[0]['version']} introduced PaymentClientV2 errors; roll back to {target}.",
            )
        if fault == "db_pool_exhaustion":
            return _call(
                "propose_remediation",
                action="scale_db_pool",
                pool_size=100,
                reason="Database connection pool is saturated and requests time out waiting for a connection.",
            )
        return _call(
            "propose_remediation",
            action="failover_payment_provider",
            reason="payments-gateway latency is near its timeout and checkout requests are timing out.",
        )

    @staticmethod
    def _answer(results):
        err = results.get("get_metrics:error_rate_pct")
        if not err:
            return "I need to look at the metrics first. Ask me what is wrong with checkout."
        fault, evidence = diagnose(results)
        if fault == "none":
            return (
                f"Checkout looks healthy: error rate is {err['current']}% against a baseline of "
                f"{err['baseline']}%, and there are no ERROR logs in the last 15 minutes."
            )
        if fault == "unknown":
            return (
                f"Checkout error rate is {err['current']}% against a baseline of {err['baseline']}%, "
                "but no log signature or recent change explains it yet. I am not proposing a fix."
            )
        lines = [
            f"Checkout error rate is {err['current']}% against a baseline of {err['baseline']}% "
            f"({err['ratio_to_baseline']}x)."
        ]
        p95 = results.get("get_metrics:p95_latency_ms")
        if p95 and fault != "bad_deploy":
            lines.append(f"p95 latency is {p95['current']}ms ({p95['current_seconds']}s).")
        deploys = results.get("get_recent_deploys", {}).get("deploys", [])
        if fault == "bad_deploy" and deploys:
            d = deploys[0]
            lines.append(f"The running version {d['version']} was deployed at {d['at']} (\"{d['summary']}\").")
        if evidence:
            lines.append(f'Logs show: "{evidence}".')
        matches = results.get("find_similar_incidents", {}).get("matches", [])
        if matches:
            m = matches[0]
            lines.append(
                f"This matches past incident {m['incident_id']} (score {m['score']}); "
                f"you fixed it before with {m['remediation']}."
            )
        proposal = results.get("propose_remediation", {})
        if proposal.get("proposal_id"):
            lines.append("I proposed a fix below. It runs only after you approve it.")
        elif proposal.get("error"):
            lines.append(f"I could not propose a fix: {proposal['error']}")
        return " ".join(lines)
