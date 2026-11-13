"""Simulated checkout API.

Everything here is a pure function of (state, time), so it runs the same under
CPython tests and inside the Python Worker. Jitter comes from a hash of
(metric, minute), which means two calls within the same minute return the same
numbers. That matters for the number check: the model quotes what a tool
returned, and a later tool call must not contradict it.
"""

import hashlib
import time

FAULTS = ("none", "bad_deploy", "db_pool_exhaustion", "latency_spike")
METRICS = ("error_rate_pct", "p95_latency_ms", "rps", "db_pool_in_use")
UNITS = {
    "error_rate_pct": "percent",
    "p95_latency_ms": "ms",
    "rps": "requests/s",
    "db_pool_in_use": "connections",
}

# The remediation that actually fixes each fault.
FIXES = {
    "bad_deploy": "rollback_deploy",
    "db_pool_exhaustion": "scale_db_pool",
    "latency_spike": "failover_payment_provider",
}

RAMP_SECONDS = 10  # fault reaches full effect this long after it starts (short for demos)
DECAY_SECONDS = 20  # matching fix brings values back to baseline over this
DEFAULT_POOL_SIZE = 50

# (low, high) ranges per fault. db_pool_in_use for db_pool_exhaustion is the
# pool size itself and is handled in metric_value().
RANGES = {
    "none": {
        "error_rate_pct": (0.3, 0.6),
        "p95_latency_ms": (180, 220),
        "rps": (420, 480),
        "db_pool_in_use": (18, 26),
    },
    "bad_deploy": {
        "error_rate_pct": (22, 26),
        "p95_latency_ms": (320, 380),
        "rps": (420, 480),
        "db_pool_in_use": (18, 26),
    },
    "db_pool_exhaustion": {
        "error_rate_pct": (8, 12),
        "p95_latency_ms": (2600, 3000),
        "rps": (520, 600),
        "db_pool_in_use": None,
    },
    "latency_spike": {
        "error_rate_pct": (2, 3),
        "p95_latency_ms": (3900, 4300),
        "rps": (420, 480),
        "db_pool_in_use": (30, 38),
    },
}

# Log signatures: id -> (level, message template). The id doubles as a
# fingerprint tag for similar-incident search.
SIGNATURES = {
    "npe_payment_client_v2": (
        "ERROR",
        "HTTP 500 POST /checkout: NullPointerException at PaymentClientV2.authorize (version {version})",
    ),
    "db_pool_timeout": (
        "ERROR",
        "HTTP 503 POST /checkout: Timeout acquiring DB connection after 30000ms (pool {pool}/{pool} in use)",
    ),
    "payments_gateway_slow": (
        "WARN",
        "payments-gateway responded in {gateway_ms}ms (timeout 4000ms) for POST /checkout",
    ),
    "payments_gateway_timeout": (
        "ERROR",
        "HTTP 504 POST /checkout: payments-gateway timed out after 4000ms",
    ),
}
FAULT_SIGNATURES = {
    "bad_deploy": ["npe_payment_client_v2"],
    "db_pool_exhaustion": ["db_pool_timeout"],
    "latency_spike": ["payments_gateway_slow", "payments_gateway_timeout"],
}
INFO_LINES = [
    "HTTP 200 POST /checkout completed in {ms}ms",
    "HTTP 200 GET /cart completed in {ms}ms",
    "payment authorized for order {order}",
    "inventory reserved for order {order}",
]


def seed_deploys(now):
    """Deploy history before any fault is triggered."""
    return [
        {
            "version": "v2.14.0",
            "at": now - 2 * 86400,
            "author": "maria.chen",
            "summary": "Add saved-cart reminder emails",
        },
        {
            "version": "v2.14.1",
            "at": now - 86400,
            "author": "dev.patel",
            "summary": "Fix tax rounding for EU orders",
        },
    ]


BAD_DEPLOY = {
    "version": "v2.15.0",
    "author": "sam.okafor",
    "summary": "Migrate payment client to PaymentClientV2",
}


def default_state():
    return {
        "version": "v2.14.1",
        "fault": "none",
        "fault_started_at": 0.0,
        "remediated_at": 0.0,
        "remediation": "",
        "db_pool_size": DEFAULT_POOL_SIZE,
    }


def _unit(*parts):
    """Deterministic float in [0, 1) from the given parts."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def fault_intensity(state, t):
    """How much of the fault's effect applies at time t, from 0.0 to 1.0."""
    if state["fault"] == "none" or t < state["fault_started_at"]:
        return 0.0
    ramp = min(1.0, (t - state["fault_started_at"]) / RAMP_SECONDS)
    fixed_at = state["remediated_at"]
    if fixed_at and state["remediation"] == FIXES[state["fault"]] and t >= fixed_at:
        ramp *= max(0.0, 1.0 - (t - fixed_at) / DECAY_SECONDS)
    return ramp


def is_recovered(state, t):
    """True once a matching fix has fully decayed the fault."""
    return (
        state["fault"] != "none"
        and bool(state["remediated_at"])
        and state["remediation"] == FIXES[state["fault"]]
        and t >= state["remediated_at"] + DECAY_SECONDS
    )


def _round(metric, value):
    return round(value, 1) if metric == "error_rate_pct" else int(round(value))


def metric_value(state, metric, t):
    minute = int(t // 60)
    low, high = RANGES["none"][metric]
    base = low + (high - low) * _unit(metric, minute, "base")
    fault = state["fault"]
    if fault == "none":
        return _round(metric, base)
    if metric == "db_pool_in_use" and fault == "db_pool_exhaustion":
        faulty = float(state["db_pool_size"])
    else:
        flow, fhigh = RANGES[fault][metric]
        faulty = flow + (fhigh - flow) * _unit(metric, minute, fault)
    k = fault_intensity(state, t)
    return _round(metric, base + (faulty - base) * k)


def metric_series(state, metric, window_minutes, now):
    """Per-minute points for the last window_minutes, oldest first."""
    points = []
    for i in range(window_minutes - 1, -1, -1):
        t = now - i * 60
        points.append({"minute": _iso_minute(t), "value": metric_value(state, metric, t)})
    values = [p["value"] for p in points]
    current = metric_value(state, metric, now)
    baseline = metric_value(default_state(), metric, now)
    result = {
        "metric": metric,
        "unit": UNITS[metric],
        "points": points,
        "current": current,
        "baseline": baseline,
        "max": max(values),
        # Derived values the model may quote; the number check only allows
        # numbers that appear in tool results, so ratios must be provided.
        "ratio_to_baseline": round(current / baseline, 1) if baseline else 0,
    }
    if metric == "p95_latency_ms":
        result["current_seconds"] = round(current / 1000, 1)
    if metric == "db_pool_in_use":
        result["pool_size"] = state["db_pool_size"]
        result["pool_utilization_pct"] = round(100 * current / state["db_pool_size"], 1)
    return result


def current_metrics(state, now):
    return {m: metric_value(state, m, now) for m in METRICS}


def _iso_minute(t):
    return time.strftime("%Y-%m-%dT%H:%M:00Z", time.gmtime(t))


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def log_lines(state, now, window_minutes=15):
    """Synthetic logs for the window, newest first. One line every 5 seconds."""
    lines = []
    for i in range(window_minutes):
        minute_start = (int(now // 60) - i) * 60
        for j in range(12):
            t = minute_start + j * 5
            if t > now:
                continue
            k = fault_intensity(state, t)
            sigs = FAULT_SIGNATURES.get(state["fault"], [])
            if sigs and _unit("log", t, j) < 0.85 * k:
                sig = sigs[int(_unit("sig", t) * len(sigs))]
                level, template = SIGNATURES[sig]
                msg = template.format(
                    version=state["version"],
                    pool=state["db_pool_size"],
                    gateway_ms=3900 + int(_unit("gw", t) * 90),
                )
                lines.append({"ts": _iso(t), "level": level, "msg": msg, "sig": sig})
            else:
                template = INFO_LINES[int(_unit("info", t) * len(INFO_LINES))]
                msg = template.format(
                    ms=120 + int(_unit("ms", t) * 80),
                    order=10000 + int(_unit("order", t) * 89999),
                )
                lines.append({"ts": _iso(t), "level": "INFO", "msg": msg, "sig": ""})
    lines.sort(key=lambda line: line["ts"], reverse=True)
    return lines


def search_logs(state, now, query="", level="", limit=20):
    matching = [
        line
        for line in log_lines(state, now)
        if (not level or line["level"] == level)
        and (not query or query.lower() in line["msg"].lower())
    ]
    return {
        "window_minutes": 15,
        "total_matching": len(matching),
        "matches": min(limit, len(matching)),
        "lines": [{k: line[k] for k in ("ts", "level", "msg")} for line in matching[:limit]],
    }


def top_error_signature(state, now):
    counts = {}
    for line in log_lines(state, now):
        if line["sig"] and line["level"] in ("ERROR", "WARN"):
            counts[line["sig"]] = counts.get(line["sig"], 0) + 1
    if not counts:
        return ""
    return max(sorted(counts), key=lambda s: counts[s])


def format_deploys(deploys, current_version, limit):
    ordered = sorted(deploys, key=lambda d: d["at"], reverse=True)[:limit]
    return {
        "current_version": current_version,
        "deploys": [
            {
                "version": d["version"],
                "at": _iso(d["at"]),
                "author": d["author"],
                "summary": d["summary"],
            }
            for d in ordered
        ],
    }
