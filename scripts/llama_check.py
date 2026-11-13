"""Live check of the real model: does Llama propose the right fix for each fault,
and no fix when checkout is healthy?

Runs against a server that uses Workers AI (`npm run dev`, or the deployed URL).
Each run gets its own sandbox, so runs don't share service state or memory.

    npm run check:llama                                  # localhost:8787, 3 runs each
    npm run check:llama -- --runs 5 --only none
    npm run check:llama -- --base https://<worker>.workers.dev

Exits 1 if any run gets the wrong outcome. Uses real Workers AI calls.
"""

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request

# Expected proposal action per fault; None means "no proposal".
EXPECTED = {
    "none": None,
    "bad_deploy": "rollback_deploy",
    "db_pool_exhaustion": "scale_db_pool",
    "latency_spike": "failover_payment_provider",
}
RAMP_SECONDS = 12  # sim.RAMP_SECONDS is 10; wait for the fault to reach full effect


def call(base, sid, method, path, body=None, timeout=180):
    req = urllib.request.Request(
        base.rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        # Cloudflare rejects urllib's default User-Agent with 403 "error code: 1010".
        headers={"content-type": "application/json", "x-sandbox-id": sid, "user-agent": "cf-ai-oncall-copilot-llama-check/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace").strip()
        try:
            return json.loads(body) | {"http_status": exc.code}
        except ValueError:
            sys.exit(f"{method} {path} -> HTTP {exc.code}: {body[:200]}")


def run_once(base, fault, message):
    sid = secrets.token_hex(6)
    if fault != "none":
        call(base, sid, "POST", "/api/service/fault", {"fault": fault})
        time.sleep(RAMP_SECONDS)
    inc = call(base, sid, "POST", "/api/incidents", {"title": "llama check: " + fault})
    reply = call(base, sid, "POST", f"/api/incidents/{inc['id']}/chat", {"message": message})
    if "message" not in reply:
        return {"error": reply.get("error", "no reply")}
    state = call(base, sid, "GET", f"/api/incidents/{inc['id']}")
    proposals = state.get("proposals", [])
    return {
        "llm_mode": state.get("llm_mode"),
        "action": proposals[0]["action"] if proposals else None,
        "model_error": next((a["detail"].get("error", "") for a in state.get("audit", []) if a["kind"] == "model_error"), None),
        "text": reply["message"]["content"],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://localhost:8787")
    ap.add_argument("--runs", type=int, default=3, help="runs per scenario (default 3)")
    ap.add_argument("--only", choices=sorted(EXPECTED), action="append", help="scenario to run; repeatable")
    ap.add_argument("--message", default="Why is checkout failing?",
                    help="question to ask; the default assumes a failure, which is the hard case for 'none'")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each answer")
    args = ap.parse_args()

    failures = 0
    for fault in args.only or list(EXPECTED):
        want = EXPECTED[fault]
        passed = 0
        for i in range(1, args.runs + 1):
            r = run_once(args.base, fault, args.message)
            if r.get("error") or r["model_error"] is not None:
                got, ok = "error: " + (r.get("error") or "model_error: " + r["model_error"][:160]), False
            elif r["llm_mode"] != "workers-ai":
                sys.exit(f"server is in {r['llm_mode']!r} mode, not workers-ai; run `npm run dev`, not dev:scripted")
            else:
                got, ok = r["action"] or "no proposal", r["action"] == want
            passed += ok
            print(f"{'PASS' if ok else 'FAIL'}  {fault:<19} run {i}: {got}")
            if args.verbose and "text" in r:
                print("      " + r["text"].replace("\n", "\n      "))
        failures += args.runs - passed
        print(f"      {fault}: {passed}/{args.runs} (want {want or 'no proposal'})\n")

    total = args.runs * len(args.only or EXPECTED)
    print(f"{total - failures}/{total} runs passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
