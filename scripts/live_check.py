#!/usr/bin/env python3
"""V6 live reliability check: run CUJ 1 (diagnose) and CUJ 4 (recall) N times
against live Llama 3.3 and report pass rates.

Usage:
  python3 scripts/live_check.py --base https://cf-ai-oncall-copilot.<you>.workers.dev
  python3 scripts/live_check.py --base http://localhost:8787   # `npm run dev` (live AI)

Standard library only. Each run gets its own sandbox, so runs don't share
faults or memory, and the 10-chats-per-minute limit is never hit.
"""

import argparse
import json
import secrets
import time
import urllib.error
import urllib.request

BAD_VERSION, GOOD_VERSION = "v2.15.0", "v2.14.1"
QUESTION = "Why is checkout failing?"


class Abort(RuntimeError):
    """A problem with the setup, not the model: stop instead of scoring it as a failed run."""


class Client:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.sid = secrets.token_hex(6)  # 12 hex chars, matches the Worker's check

    def call(self, path, body=None, timeout=180):
        req = urllib.request.Request(
            self.base + path,
            data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "POST",
            # Cloudflare rejects urllib's default User-Agent with 403 "error code: 1010".
            headers={"content-type": "application/json", "x-sandbox-id": self.sid,
                     "user-agent": "cf-ai-oncall-copilot-live-check/1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise Abort(f"{path} -> HTTP {e.code}: {e.read()[:200]!r}") from None


def trigger_bad_deploy(c):
    c.call("/api/service/fault", {"fault": "bad_deploy"})
    for _ in range(30):  # README: error rate turns red within about 10 s
        if c.call("/api/service")["metrics"]["error_rate_pct"] > 5:
            return
        time.sleep(1)
    raise Abort("error rate never rose after triggering bad_deploy")


def diagnose(c):
    """Open an incident, ask the question, return (answer message, state)."""
    inc = c.call("/api/incidents", {"title": "Checkout 500s"})
    reply = c.call(f"/api/incidents/{inc['id']}/chat", {"message": QUESTION})["message"]
    state = c.call(f"/api/incidents/{inc['id']}")
    mode = reply.get("meta", {}).get("llm_mode")
    if mode == "scripted-fallback":
        raise Abort("Workers AI daily quota is used up (answer came from the scripted fallback); try after 00:00 UTC")
    if mode != "workers-ai":
        raise Abort(f"server answered in {mode!r} mode; point --base at a Worker using Workers AI")
    return inc["id"], reply, state


def judge(reply, state, need_recall):
    """V6 pass criteria: correct version named, number check passed, right fix proposed."""
    text, meta = reply.get("content", ""), reply.get("meta", {})
    props = [p for p in state.get("proposals", []) if p.get("action") == "rollback_deploy"]
    checks = {
        "live_model": meta.get("llm_mode") == "workers-ai",
        "names_bad_version": BAD_VERSION in text,
        "numbers_verified": bool(meta.get("number_check", {}).get("ok")),
        "right_fix": any(p.get("params", {}).get("target_version") == GOOD_VERSION for p in props),
    }
    if need_recall:
        low = text.lower()
        checks["recalls_past_incident"] = "inc-" in low or "past incident" in low or "before" in low
    return all(checks.values()), checks


def wait_resolved(c, incident_id, proposal_id, timeout=180):
    c.call(f"/api/incidents/{incident_id}/proposals/{proposal_id}/approve", {})
    end = time.time() + timeout
    while time.time() < end:
        st = c.call(f"/api/incidents/{incident_id}")
        status = next((p["status"] for p in st["proposals"] if p["id"] == proposal_id), "missing")
        if status in ("succeeded", "failed", "stale", "expired", "rejected"):
            return status
        time.sleep(2)
    return "timeout"


def run_once(base, i):
    c = Client(base)
    c.call("/api/service/reset", {})
    trigger_bad_deploy(c)

    # CUJ 1: diagnose a bad deploy
    inc1, reply1, state1 = diagnose(c)
    ok1, checks1 = judge(reply1, state1, need_recall=False)
    result = {"run": i, "cuj1": ok1, "cuj1_checks": checks1, "cuj1_answer": reply1.get("content", "")}

    # CUJ 4 needs a resolved incident in memory first: approve CUJ 1's fix.
    prop = next((p for p in state1["proposals"] if p.get("action") == "rollback_deploy"), None)
    if not prop:
        result.update(cuj4=False, cuj4_checks={"setup": "no rollback proposal to approve in CUJ 1"})
        return result
    status = wait_resolved(c, inc1, prop["id"])
    if status != "succeeded":
        result.update(cuj4=False, cuj4_checks={"setup": f"remediation ended {status}"})
        return result

    # CUJ 4: same fault again, new incident, should recall the past fix
    trigger_bad_deploy(c)
    _, reply4, state4 = diagnose(c)
    ok4, checks4 = judge(reply4, state4, need_recall=True)
    result.update(cuj4=ok4, cuj4_checks=checks4, cuj4_answer=reply4.get("content", ""))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="deployed URL or http://localhost:8787")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--out", default="live_check_results.json", help="per-run details (gitignored)")
    args = ap.parse_args()

    results = []
    for i in range(1, args.runs + 1):
        try:
            r = run_once(args.base, i)
        except Abort as exc:
            print(f"run {i:2}: stopped: {exc}")
            if results:
                print(f"{len(results)} completed run(s) not reported; rerun for a full result.")
            raise SystemExit(2)
        results.append(r)
        fails = {k: v for k, v in {**r.get("cuj1_checks", {}), **r.get("cuj4_checks", {})}.items() if v is not True}
        print(f"run {i:2}: CUJ1 {'PASS' if r['cuj1'] else 'FAIL'}  CUJ4 {'PASS' if r['cuj4'] else 'FAIL'}"
              + (f"  {fails}" if fails else ""))

    p1 = sum(r["cuj1"] for r in results)
    p4 = sum(r["cuj4"] for r in results)
    n = len(results)
    with open(args.out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nCUJ 1 (diagnose): {p1}/{n}   CUJ 4 (recall): {p4}/{n}   details: {args.out}")
    print(f"\nREADME row:\n| Live Llama 3.3, {n} runs each (design V6): correct version, numbers verified, "
          f"right fix | CUJ 1: {p1}/{n}, CUJ 4: {p4}/{n} ({time.strftime('%Y-%m-%d')}) |")


if __name__ == "__main__":
    main()