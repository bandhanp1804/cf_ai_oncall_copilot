"""Tool definitions sent to the model.

Schemas stay flat (scalar properties only, at most four, enums for closed
sets) because Llama 3.3 fills flat schemas far more reliably than nested ones.
The same dicts drive guardrails.validate_args, so what the model is told and
what is enforced cannot drift apart.

Every tool except propose_remediation is read-only. propose_remediation only
records a proposal and starts a Workflow that waits for human approval.
"""

import sim

ACTIONS = ("rollback_deploy", "scale_db_pool", "failover_payment_provider")

TOOLS = [
    {
        "name": "get_metrics",
        "description": "Get per-minute values of one checkout metric for the last N minutes, plus current, baseline and max.",
        "parameters": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "enum": list(sim.METRICS),
                    "description": "Which metric to read.",
                },
                "window_minutes": {
                    "type": "integer",
                    "minimum": 5,
                    "maximum": 60,
                    "default": 15,
                    "description": "How many minutes back to read.",
                },
            },
            "required": ["metric"],
        },
    },
    {
        "name": "search_logs",
        "description": "Search checkout logs from the last 15 minutes. Filter by a case-insensitive substring and/or level.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": 100,
                    "description": "Substring to match, e.g. 'Exception' or 'timeout'. Leave empty for all lines.",
                },
                "level": {
                    "type": "string",
                    "enum": ["ERROR", "WARN", "INFO"],
                    "description": "Only return lines at this level.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 20,
                    "description": "Maximum lines to return.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_deploys",
        "description": "List recent checkout deploys, newest first, and the version currently running.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                    "default": 5,
                    "description": "How many deploys to return.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_similar_incidents",
        "description": "Find past resolved checkout incidents whose symptoms match the current telemetry, with how they were fixed.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "Optional short description of the symptoms.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "propose_remediation",
        "description": (
            "Propose ONE fix for a human to approve. This does not change anything by itself; "
            "the fix runs only after the on-call engineer clicks Approve. "
            "rollback_deploy needs target_version; scale_db_pool needs pool_size."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(ACTIONS),
                    "description": "The fix to propose.",
                },
                "target_version": {
                    "type": "string",
                    "maxLength": 20,
                    "description": "For rollback_deploy: the version to roll back to, e.g. v2.14.1.",
                },
                "pool_size": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 200,
                    "description": "For scale_db_pool: the new connection pool size.",
                },
                "reason": {
                    "type": "string",
                    "maxLength": 300,
                    "description": "One sentence: the root cause this fix addresses, citing the evidence.",
                },
            },
            "required": ["action", "reason"],
        },
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
READ_ONLY = {"get_metrics", "search_logs", "get_recent_deploys", "find_similar_incidents"}


def model_tools():
    """Tool list in the format Workers AI accepts for function calling."""
    return [
        {"type": "function", "function": {k: t[k] for k in ("name", "description", "parameters")}}
        for t in TOOLS
    ]
