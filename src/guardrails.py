"""Guardrails: tool-argument validation and the number check.

Both run in code, outside the model. Neither trusts the model's output.
"""

import json
import re


class ToolArgError(ValueError):
    pass


def validate_args(schema, args):
    """Validate and normalize tool arguments against a flat JSON schema.

    Returns a new dict with defaults filled in and unknown keys dropped.
    Raises ToolArgError with a message the model can act on.
    """
    if args is None:
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            raise ToolArgError("arguments must be a JSON object") from None
    if not isinstance(args, dict):
        raise ToolArgError("arguments must be a JSON object")

    props = schema.get("properties", {})
    out = {}
    for name, spec in props.items():
        if name not in args or args[name] is None or args[name] == "":
            if name in schema.get("required", []):
                raise ToolArgError(f"missing required argument '{name}'")
            if "default" in spec:
                out[name] = spec["default"]
            continue
        out[name] = _coerce(name, spec, args[name])
    return out


def _coerce(name, spec, value):
    kind = spec.get("type")
    if kind == "integer":
        if isinstance(value, bool):
            raise ToolArgError(f"'{name}' must be an integer, got a boolean")
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            value = int(value.strip())
        elif isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ToolArgError(f"'{name}' must be an integer, got {value!r}")
        if "minimum" in spec and value < spec["minimum"]:
            raise ToolArgError(f"'{name}' must be >= {spec['minimum']}, got {value}")
        if "maximum" in spec and value > spec["maximum"]:
            raise ToolArgError(f"'{name}' must be <= {spec['maximum']}, got {value}")
    elif kind == "string":
        if not isinstance(value, str):
            raise ToolArgError(f"'{name}' must be a string, got {value!r}")
        value = value.strip()
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            value = value[: spec["maxLength"]]
    if "enum" in spec and value not in spec["enum"]:
        raise ToolArgError(f"'{name}' must be one of {spec['enum']}, got {value!r}")
    return value


# Numbers in prose: 1,234.5 / 23.8% / 3900ms / -3. Version strings, ids,
# dates, times and HTTP status codes are removed before matching, so their
# digits are not checked.
_SKIP = re.compile(
    r"\bv?\d+\.\d+\.\d+\b"  # v2.15.0 / 2.15.0
    r"|\b(?:inc|prop)-[0-9a-f]+\b"  # inc-1a2b3c4d, prop-...
    r"|\b\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?"  # ISO dates and timestamps
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"  # 14:05 / 14:05:30
    r"|\b(?:HTTP|status) \d{3}\b|\b[45]\d{2}s\b|\b[45]\d{2} errors\b"  # HTTP 500, 500s
    r"|\bp\d{2}\b",  # p95, p99
    re.IGNORECASE,
)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?")


def _number_spans(text):
    """(start, end, normalized) for each checkable number in text."""
    skipped = [m.span() for m in _SKIP.finditer(text)]
    spans = []
    for m in _NUMBER.finditer(text):
        if any(s <= m.start() < e for s, e in skipped):
            continue
        spans.append((m.start(), m.end(), m.group(0).replace(",", "")))
    return spans


def extract_numbers(text):
    return [n for _, _, n in _number_spans(text)]


def redact_numbers(text, unverified):
    """Replace each unverified number in text with [unverified]."""
    bad = set(unverified)
    out, last = [], 0
    for start, end, n in _number_spans(text):
        if n in bad:
            out.append(text[last:start])
            out.append("[unverified]")
            last = end
    out.append(text[last:])
    return "".join(out)


# Result fields whose numbers must not count as evidence: timestamps and ids
# (they contain arbitrary digits) and free text the model itself wrote
# earlier (a past proposal's reason becomes a stored root_cause).
SKIP_KEYS = {"ts", "minute", "at", "resolved_at", "incident_id", "proposal_id", "root_cause", "title", "reason"}


def _numbers_in(obj, into):
    """Collect every number in a tool result, including inside strings such as log lines."""
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        into.add(float(obj))
    elif isinstance(obj, str):
        for raw in re.findall(r"-?\d+(?:\.\d+)?", obj.replace(",", "")):
            into.add(float(raw))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in SKIP_KEYS:
                _numbers_in(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, into)


def allowed_numbers(sources):
    allowed = set(float(i) for i in range(11)) | {100.0}
    for source in sources:
        _numbers_in(source, allowed)
    return allowed


def _decimals(raw):
    return len(raw.split(".")[1]) if "." in raw else 0


def number_is_grounded(raw, allowed):
    value = float(raw)
    places = _decimals(raw)
    for a in allowed:
        if a == value or round(a, places) == value:
            return True
    return False


def check_numbers(text, sources):
    """Return {"ok": bool, "unverified": [str]} for the numbers quoted in text.

    sources: tool results and user messages for this incident (never tool
    arguments). A quoted number
    passes if it equals a source number, or equals a source number rounded to
    the quoted precision (24 matches 23.8, 23.8 matches 23.84).
    """
    allowed = allowed_numbers(sources)
    unverified = []
    for raw in extract_numbers(text):
        if not number_is_grounded(raw, allowed) and raw not in unverified:
            unverified.append(raw)
    return {"ok": not unverified, "unverified": unverified}


# A metric this far above baseline counts as a symptom. Healthy readings sit
# at 1.0 (sim compares against the same minute with no fault); every fault
# pushes at least one metric well past 1.5.
PROBLEM_RATIO = 1.5
PROBLEM_LEVELS = {"ERROR", "WARN"}

NO_PROBLEM = (
    "no evidence of a problem: every metric checked is within "
    f"{PROBLEM_RATIO}x of baseline and no ERROR or WARN logs were found. Check "
    "the other metrics (p95_latency_ms, db_pool_in_use) and search_logs. If they "
    "are normal too, tell the engineer checkout looks healthy and do not propose a fix."
)


def shows_problem(sources):
    """True if any tool result is a symptom: an elevated metric or ERROR/WARN logs.

    Proposals are gated on this in code because the model, asked "why is
    checkout failing?", will propose a fix for a healthy service.
    """
    for s in sources:
        if not isinstance(s, dict):
            continue
        ratio = s.get("ratio_to_baseline")
        if isinstance(ratio, (int, float)) and not isinstance(ratio, bool) and ratio >= PROBLEM_RATIO:
            return True
        if any(isinstance(line, dict) and line.get("level") in PROBLEM_LEVELS for line in s.get("lines") or []):
            return True
    return False
