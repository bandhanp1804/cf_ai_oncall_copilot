"""Similar-incident search.

The fingerprint is computed in code from telemetry, not from the model's
description, so a vague or wrong summary from the model cannot change which
past incident matches.
"""

import re

import sim

MATCH_THRESHOLD = 0.5
RECENT_DEPLOY_SECONDS = 30 * 60


def fingerprint(state, deploys, now):
    metrics = sim.current_metrics(state, now)
    tags = set()
    if metrics["error_rate_pct"] > 5:
        tags.add("high_error_rate")
    if metrics["p95_latency_ms"] > 1000:
        tags.add("high_latency")
    if metrics["db_pool_in_use"] >= 0.95 * state["db_pool_size"]:
        tags.add("pool_saturated")
    current = [d for d in deploys if d["version"] == state["version"]]
    if current and now - current[0]["at"] < RECENT_DEPLOY_SECONDS:
        tags.add("recent_deploy")
    sig = sim.top_error_signature(state, now)
    if sig:
        tags.add("sig:" + sig)
    return sorted(tags)


def _words(text):
    return {w for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 4}


def score(current_tags, past_tags, query="", past_title=""):
    a, b = set(current_tags), set(past_tags)
    if not a and not b:
        return 0.0
    s = len(a & b) / len(a | b)
    if query and _words(query) & _words(past_title):
        s += 0.1
    return round(min(1.0, s), 2)


def find_similar(current_tags, past, query="", limit=3):
    """past: dicts with incident_id, title, tags (list), root_cause, remediation, resolved_at."""
    scored = []
    for p in past:
        s = score(current_tags, p["tags"], query, p["title"])
        if s >= MATCH_THRESHOLD:
            scored.append({**{k: p[k] for k in p if k != "tags"}, "score": s, "tags": p["tags"]})
    scored.sort(key=lambda m: (-m["score"], -m["resolved_at"]))
    return scored[:limit]
