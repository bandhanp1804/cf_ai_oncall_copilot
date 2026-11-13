"""In-memory stand-ins for CheckoutService and IncidentAgent, built on ops."""

import ops
import sim

NOW = 1_800_000_000.0


class FakeService:
    def __init__(self, now=NOW):
        self.now = now
        self.state = sim.default_state()
        self.deploys = sim.seed_deploys(now)
        self.past = []
        self.proposals = []

    def trigger(self, fault, ago=120):
        t = self.now - ago
        self.state.update(fault=fault, fault_started_at=t, remediated_at=0.0, remediation="")
        if fault == "bad_deploy":
            self.deploys = [d for d in self.deploys if d["version"] != "v2.15.0"]
            self.deploys.append({**sim.BAD_DEPLOY, "at": t})
            self.state["version"] = "v2.15.0"

    async def execute_tool(self, name, args):
        if name == "propose_remediation":
            err = ops.validate_remediation(args, self.state, self.deploys)
            if err:
                return {"error": err}
            pid = f"prop-{len(self.proposals):08x}"
            self.proposals.append({**args, "proposal_id": pid})
            return {"proposal_id": pid, "status": "pending_approval", "action": ops.describe(args)}
        return ops.run_read_tool(name, args, self.state, self.deploys, self.past, self.now)


class Audit:
    def __init__(self):
        self.entries = []

    def __call__(self, kind, detail):
        self.entries.append((kind, detail))

    def kinds(self):
        return [k for k, _ in self.entries]
