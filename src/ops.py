"""CheckoutService logic over plain dicts: read-only tools and remediation.

The Durable Object loads state from SQLite, calls these, and saves what they
return. Keeping the decisions here lets tests cover them under CPython.
"""

import memory
import sim

TERMINAL = {"rejected", "succeeded", "failed", "stale", "expired"}


def run_read_tool(name, args, state, deploys, past, now):
    """Dispatch one read-only tool. past: rows for memory.find_similar."""
    if name == "get_metrics":
        return sim.metric_series(state, args["metric"], args["window_minutes"], now)
    if name == "search_logs":
        return sim.search_logs(state, now, args.get("query", ""), args.get("level", ""), args["limit"])
    if name == "get_recent_deploys":
        return sim.format_deploys(deploys, state["version"], args["limit"])
    if name == "find_similar_incidents":
        tags = memory.fingerprint(state, deploys, now)
        matches = memory.find_similar(tags, past, args.get("query", ""))
        return {
            "fingerprint": tags,
            "matches": [
                {
                    "incident_id": m["incident_id"],
                    "title": m["title"],
                    "score": m["score"],
                    "root_cause": m["root_cause"],
                    "remediation": m["remediation"],
                    "resolved_at": sim._iso(m["resolved_at"]),
                }
                for m in matches
            ],
        }
    raise ValueError(f"not a read-only tool: {name}")


def validate_remediation(p, state, deploys):
    """Return an error string, or "" if the proposal is valid against current state."""
    action = p["action"]
    if action == "rollback_deploy":
        target = p.get("target_version", "")
        if not target:
            return "rollback_deploy needs target_version"
        by_version = {d["version"]: d for d in deploys}
        if target not in by_version:
            return f"unknown version {target}; known: {sorted(by_version)}"
        if target == state["version"]:
            return f"{target} is already running"
        current = by_version.get(state["version"])
        if current and by_version[target]["at"] >= current["at"]:
            return f"{target} is not older than the running version {state['version']}"
    elif action == "scale_db_pool":
        size = p.get("pool_size") or 0
        if not size:
            return "scale_db_pool needs pool_size"
        if size <= state["db_pool_size"]:
            return f"pool_size must be larger than the current size {state['db_pool_size']}"
    elif action != "failover_payment_provider":
        return f"unknown action {action}"
    return ""


def describe(p):
    if p["action"] == "rollback_deploy":
        return f"rollback_deploy to {p['target_version']}"
    if p["action"] == "scale_db_pool":
        return f"scale_db_pool to {p['pool_size']} connections"
    return "failover_payment_provider"


def apply(p, state, deploys, now):
    """Compare-and-set apply. Returns (new_state or None, result dict).

    The proposal carries the version and fault seen at proposal time. If
    either changed (Reset, another fault, another incident's fix), nothing is
    applied and the result is {"error": "stale", ...}.
    """
    if state["version"] != p["expected_version"] or state["fault"] != p["expected_fault"]:
        return None, {
            "error": "stale",
            "detail": f"expected {p['expected_version']}/{p['expected_fault']}, found {state['version']}/{state['fault']}",
        }
    err = validate_remediation(p, state, deploys)
    if err:
        return None, {"error": "invalid", "detail": err}
    new = dict(state)
    if p["action"] == "rollback_deploy":
        new["version"] = p["target_version"]
    elif p["action"] == "scale_db_pool":
        new["db_pool_size"] = p["pool_size"]
    new["remediation"] = p["action"]
    new["remediated_at"] = now
    return new, {"ok": True, "applied": describe(p), "version": new["version"]}


def settle(state, now):
    """Clear a fault once a matching fix has fully taken effect."""
    if sim.is_recovered(state, now):
        new = dict(state)
        new.update(fault="none", fault_started_at=0.0, remediated_at=0.0, remediation="")
        return new
    return state


def healthy(metrics):
    return metrics["error_rate_pct"] < 1.0 and metrics["p95_latency_ms"] < 500
