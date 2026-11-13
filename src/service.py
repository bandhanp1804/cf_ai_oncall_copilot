"""CheckoutService: the simulated service, its deploy history, and incident memory.

One object per sandbox (name "checkout-<sid>"). Every RPC method takes and
returns a JSON string. apply_remediation is the only method that changes
service state on behalf of a fix, and only RemediationWorkflow calls it.
"""

import json
import time

from workers import DurableObject

import memory
import ops
import sim
from db import Db

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS deploys (version TEXT PRIMARY KEY, at REAL, author TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS memory (
  incident_id TEXT PRIMARY KEY, title TEXT, tags TEXT, root_cause TEXT, remediation TEXT, resolved_at REAL);
CREATE TABLE IF NOT EXISTS incident_index (id TEXT PRIMARY KEY, title TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS remediations (proposal_id TEXT PRIMARY KEY, result TEXT, at REAL)
"""


class CheckoutService(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.db = Db(ctx.storage.sql)
        self.db.script(SCHEMA)
        if not self.db.one("SELECT key FROM state WHERE key = 'state'"):
            self._seed(time.time())

    # -- storage --------------------------------------------------------

    def _seed(self, now):
        self.db.run("DELETE FROM deploys")
        for d in sim.seed_deploys(now):
            self._upsert_deploy(d)
        self._save(sim.default_state())

    def _upsert_deploy(self, d):
        self.db.run(
            "INSERT OR REPLACE INTO deploys (version, at, author, summary) VALUES (?, ?, ?, ?)",
            d["version"], d["at"], d["author"], d["summary"],
        )

    def _save(self, state):
        self.db.run("INSERT OR REPLACE INTO state (key, value) VALUES ('state', ?)", json.dumps(state))

    def _state(self, now):
        row = self.db.one("SELECT value FROM state WHERE key = 'state'")
        state = json.loads(row["value"]) if row else sim.default_state()
        settled = ops.settle(state, now)
        if settled is not state:
            self._save(settled)
        return settled

    def _deploys(self):
        return self.db.rows("SELECT version, at, author, summary FROM deploys")

    def _past(self):
        return [
            {**r, "tags": json.loads(r["tags"])}
            for r in self.db.rows("SELECT incident_id, title, tags, root_cause, remediation, resolved_at FROM memory")
        ]

    # -- read-only ------------------------------------------------------

    async def status(self):
        now = time.time()
        state = self._state(now)
        return json.dumps({
            "version": state["version"],
            "fault": state["fault"],
            "db_pool_size": state["db_pool_size"],
            "metrics": sim.current_metrics(state, now),
            "deploys": sim.format_deploys(self._deploys(), state["version"], 5)["deploys"],
            "memory_count": len(self._past()),
        })

    async def tool(self, payload):
        """Run one read-only tool. payload: {"name", "args"}."""
        p = json.loads(payload)
        now = time.time()
        result = ops.run_read_tool(p["name"], p["args"], self._state(now), self._deploys(), self._past(), now)
        return json.dumps(result)

    async def snapshot(self):
        """Version, fault, metrics and fingerprint at this moment."""
        now = time.time()
        state = self._state(now)
        deploys = self._deploys()
        return json.dumps({
            "version": state["version"],
            "fault": state["fault"],
            "metrics": sim.current_metrics(state, now),
            "fingerprint": memory.fingerprint(state, deploys, now),
            "at": now,
        })

    async def validate(self, payload):
        """Check a proposed fix against current state without applying it."""
        p = json.loads(payload)
        state = self._state(time.time())
        return json.dumps({"error": ops.validate_remediation(p, state, self._deploys())})

    # -- the one write path for fixes -----------------------------------

    async def apply_remediation(self, payload):
        p = json.loads(payload)
        done = self.db.one("SELECT result FROM remediations WHERE proposal_id = ?", p["proposal_id"])
        if done:
            return done["result"]  # idempotent: Workflow retries must not re-apply

        agent = self.env.INCIDENT_AGENT.getByName(p["incident_id"])
        status = json.loads(await agent.proposal_status(json.dumps({"proposal_id": p["proposal_id"]})))
        if status.get("status") not in ("approving", "applying"):
            return json.dumps({"error": "not_approved", "detail": f"proposal status is {status.get('status')}"})

        now = time.time()
        new_state, result = ops.apply(p, self._state(now), self._deploys(), now)
        if new_state is not None:
            self._save(new_state)
        out = json.dumps(result)
        self.db.run("INSERT OR REPLACE INTO remediations (proposal_id, result, at) VALUES (?, ?, ?)", p["proposal_id"], out, now)
        return out

    # -- demo controls (UI only; never exposed as tools) ----------------

    async def set_fault(self, payload):
        fault = json.loads(payload)["fault"]
        if fault not in sim.FAULTS:
            return json.dumps({"error": f"unknown fault {fault}"})
        now = time.time()
        state = self._state(now)
        state.update(fault=fault, fault_started_at=now, remediated_at=0.0, remediation="")
        if fault == "bad_deploy":
            self._upsert_deploy({**sim.BAD_DEPLOY, "at": now})
            state["version"] = sim.BAD_DEPLOY["version"]
        self._save(state)
        return await self.status()

    async def reset(self):
        """Back to a healthy service. Incident memory is kept on purpose."""
        self._seed(time.time())
        return await self.status()

    # -- memory and incident index --------------------------------------

    async def remember(self, payload):
        p = json.loads(payload)
        self.db.run(
            "INSERT OR REPLACE INTO memory (incident_id, title, tags, root_cause, remediation, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            p["incident_id"], p["title"], json.dumps(p["tags"]), p["root_cause"], p["remediation"], p["resolved_at"],
        )
        return json.dumps({"ok": True})

    async def index_incident(self, payload):
        p = json.loads(payload)
        self.db.run(
            "INSERT INTO incident_index (id, title, status, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, status = excluded.status",
            p["id"], p["title"], p["status"], p["created_at"],
        )
        return json.dumps({"ok": True})

    async def list_incidents(self):
        return json.dumps(self.db.rows("SELECT id, title, status, created_at FROM incident_index ORDER BY created_at DESC"))
