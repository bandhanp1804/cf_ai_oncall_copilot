"""RemediationWorkflow: the only path that changes the service for a fix.

snapshot -> report_awaiting -> wait for approval -> apply -> verify -> report.

run() is replayed from the top whenever the instance resumes, and only
step.do results are memoized. So every side effect (including each report to
the IncidentAgent) is its own uniquely named step, and step functions take no
parameters (values come from closures), which works with or without the
python_workflows_implicit_dependencies flag.
"""

import json

from workers import WorkflowEntrypoint

import ops

APPLY_CONFIG = {"retries": {"limit": 3, "delay": "2 seconds", "backoff": "exponential"}}
# With the simulator's 20 s decay: attempts at ~0, 3, 9 and 21 s; the last should pass.
VERIFY_CONFIG = {"retries": {"limit": 4, "delay": "3 seconds", "backoff": "exponential"}, "timeout": "30 seconds"}


class RemediationWorkflow(WorkflowEntrypoint):
    async def run(self, event, step):
        p = dict(event["payload"])
        env = self.env

        def agent():
            return env.INCIDENT_AGENT.getByName(p["incident_id"])

        def service():
            return env.CHECKOUT_SERVICE.getByName("checkout-" + p["sid"])

        def reporter(name, status, detail=None):
            @step.do(name)
            async def report():
                msg = {"proposal_id": p["proposal_id"], "step": name, "status": status, "detail": detail or {}}
                return json.loads(await agent().workflow_update(json.dumps(msg)))

            return report

        @step.do("snapshot")
        async def snapshot():
            return json.loads(await service().snapshot())

        snap = await snapshot()
        await reporter("report_awaiting", "awaiting_approval", {"metrics": snap["metrics"]})()

        try:
            decision = await step.wait_for_event("wait for approval", "approval", timeout="1 hour")
            approved = bool(dict(decision["payload"]).get("approved"))
        except Exception:
            await reporter("report_expired", "expired")()
            return {"status": "expired"}
        if not approved:
            await reporter("report_rejected", "rejected")()
            return {"status": "rejected"}

        await reporter("report_applying", "applying")()

        @step.do("apply", config=APPLY_CONFIG)
        async def apply():
            # Business errors (stale state, not approved, invalid) come back as
            # a result so they are memoized and not retried; only transport
            # failures raise and get retried. apply_remediation is idempotent
            # per proposal_id, so a retry after a lost response is safe.
            return json.loads(await service().apply_remediation(json.dumps(p)))

        applied = await apply()
        if applied.get("error"):
            await reporter("report_stale", "stale", {"detail": applied.get("detail", applied["error"])})()
            return {"status": "stale"}

        await reporter("report_verifying", "verifying", {"applied": applied.get("applied", "")})()

        @step.do("verify", config=VERIFY_CONFIG)
        async def verify():
            metrics = json.loads(await service().snapshot())["metrics"]
            if not ops.healthy(metrics):
                raise Exception(f"not healthy yet: {json.dumps(metrics)}")
            return metrics

        try:
            metrics = await verify()
        except Exception:

            @step.do("final_snapshot")
            async def final_snapshot():
                return json.loads(await service().snapshot())["metrics"]

            last = await final_snapshot()
            await reporter("report_failed", "failed", {"metrics": last})()
            return {"status": "failed"}

        await reporter("report_succeeded", "succeeded", {"metrics": metrics})()
        return {"status": "succeeded"}
