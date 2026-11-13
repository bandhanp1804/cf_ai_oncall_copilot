"""IncidentAgent: one Durable Object per incident.

Owns the conversation, findings, proposals and audit log. Runs the chat turn
through Copilot. Every RPC method takes and returns a JSON string.
"""

import json
import secrets
import time

from workers import DurableObject

import ops
from copilot import Copilot
from db import Db
from scripted import ScriptedModel

MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
PROPOSAL_TTL_SECONDS = 65 * 60
OPEN_STATUSES = ("creating", "awaiting_approval", "approving", "applying", "verifying")
# Workers AI error for "daily free allocation used up". The turn then falls
# back to the scripted planner so the demo keeps working until the quota resets.
QUOTA_ERROR = "4006"

SCHEMA = """
CREATE TABLE IF NOT EXISTS incident (id TEXT PRIMARY KEY, sid TEXT, title TEXT, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT, meta TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS findings (id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT, args TEXT, result TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, detail TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, action TEXT, params TEXT, reason TEXT, fingerprint TEXT, status TEXT,
  workflow_id TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS workflow_steps (proposal_id TEXT, step TEXT, at REAL, PRIMARY KEY (proposal_id, step))
"""

NOTICES = {
    "succeeded": "Remediation succeeded: {applied}. Verified error rate {error_rate_pct}% and p95 {p95_latency_ms}ms. Incident resolved and saved to memory.",
    "failed": "Remediation {applied} was applied, but verification failed: error rate is still {error_rate_pct}% and p95 {p95_latency_ms}ms. The incident stays open.",
    "stale": "The fix was not applied because the service changed after it was proposed ({detail}). Ask me to re-check.",
    "expired": "The proposal expired without a decision. Nothing was changed.",
    "rejected": "Proposal rejected. Nothing was changed.",
}


class IncidentAgent(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.db = Db(ctx.storage.sql)
        self.db.script(SCHEMA)
        self.busy = False
        self.queued_notices = []

    # -- helpers ----------------------------------------------------------

    def _incident(self):
        return self.db.one("SELECT id, sid, title, status, created_at FROM incident")

    def _service(self, sid):
        return self.env.CHECKOUT_SERVICE.getByName("checkout-" + sid)

    def _audit(self, kind, detail):
        self.db.run(
            "INSERT INTO audit_log (kind, detail, created_at) VALUES (?, ?, ?)",
            kind, json.dumps(detail, default=str), time.time(),
        )

    def _add_message(self, role, content, meta=None):
        self.db.run(
            "INSERT INTO messages (role, content, meta, created_at) VALUES (?, ?, ?, ?)",
            role, content, json.dumps(meta or {}), time.time(),
        )

    def _notice(self, text, meta=None):
        # Workflow updates can land while a chat turn is awaiting the model;
        # hold them so they appear after that turn's answer, not inside it.
        if self.busy:
            self.queued_notices.append((text, meta))
        else:
            self._add_message("notice", text, meta)

    async def _set_status(self, status):
        inc = self._incident()
        self.db.run("UPDATE incident SET status = ?", status)
        await self._service(inc["sid"]).index_incident(json.dumps({**inc, "status": status}))

    def _proposal(self, pid):
        row = self.db.one("SELECT * FROM proposals WHERE id = ?", pid)
        if row:
            row["params"] = json.loads(row["params"])
            row["fingerprint"] = json.loads(row["fingerprint"])
        return row

    def _var(self, name):
        try:
            value = getattr(self.env, name)
        except Exception:
            return ""
        return value if isinstance(value, str) else ""

    def _llm_mode(self):
        return self._var("LLM_MODE") or "workers-ai"

    def _owns(self, sid):
        inc = self._incident()
        return inc is not None and inc["sid"] == sid

    # -- lifecycle --------------------------------------------------------

    async def init(self, payload):
        p = json.loads(payload)
        if not self._incident():
            now = time.time()
            self.db.run(
                "INSERT INTO incident (id, sid, title, status, created_at) VALUES (?, ?, ?, 'open', ?)",
                p["id"], p["sid"], p["title"], now,
            )
            await self._service(p["sid"]).index_incident(
                json.dumps({"id": p["id"], "title": p["title"], "status": "open", "created_at": now})
            )
            self._audit("incident_opened", {"title": p["title"]})
        return json.dumps(self._incident())

    async def get_state(self, payload):
        p = json.loads(payload)
        if not self._owns(p["sid"]):
            return json.dumps({"error": "not found"})
        await self._sweep_expired()
        messages = []
        for m in self.db.rows("SELECT id, role, content, meta, created_at FROM messages ORDER BY id"):
            messages.append({**m, "meta": json.loads(m["meta"] or "{}")})
        audit = [
            {**a, "detail": json.loads(a["detail"])}
            for a in self.db.rows("SELECT id, kind, detail, created_at FROM audit_log ORDER BY id DESC LIMIT 200")
        ]
        proposals = [self._proposal(r["id"]) for r in self.db.rows("SELECT id FROM proposals ORDER BY created_at")]
        steps = self.db.rows("SELECT proposal_id, step, at FROM workflow_steps ORDER BY at")
        for prop in proposals:
            prop["steps"] = [s["step"] for s in steps if s["proposal_id"] == prop["id"]]
        return json.dumps({
            "incident": self._incident(),
            "messages": messages,
            "audit": audit,
            "proposals": proposals,
            "busy": self.busy,
            "llm_mode": self._llm_mode(),
            "fallback_url": self._var("FALLBACK_URL"),
        })

    async def _sweep_expired(self):
        cutoff = time.time() - PROPOSAL_TTL_SECONDS
        for r in self.db.rows(
            "SELECT id FROM proposals WHERE status IN ('creating', 'awaiting_approval') AND created_at < ?", cutoff
        ):
            self.db.run("UPDATE proposals SET status = 'expired', updated_at = ? WHERE id = ?", time.time(), r["id"])
            self._audit("workflow_step", {"proposal_id": r["id"], "step": "sweep", "status": "expired"})

    # -- chat -------------------------------------------------------------

    async def chat(self, payload):
        p = json.loads(payload)
        if not self._owns(p["sid"]):
            return json.dumps({"error": "not found"})
        if self.busy:
            return json.dumps({"error": "busy"})
        self.busy = True
        try:
            return json.dumps({"message": await self._turn(p["message"])})
        finally:
            self.busy = False
            for text, meta in self.queued_notices:
                self._add_message("notice", text, meta)
            self.queued_notices = []

    async def _turn(self, text):
        inc = self._incident()
        if not self.db.one("SELECT id FROM messages WHERE role = 'user'"):
            title = text.strip().splitlines()[0][:80] if text.strip() else inc["title"]
            self.db.run("UPDATE incident SET title = ?", title)
            await self._service(inc["sid"]).index_incident(json.dumps({**inc, "title": title}))

        history = []
        for m in self.db.rows("SELECT role, content FROM messages ORDER BY id DESC LIMIT 12")[::-1]:
            if m["role"] == "notice":
                history.append({"role": "assistant", "content": "[Remediation update] " + m["content"]})
            else:
                history.append({"role": m["role"], "content": m["content"]})
        evidence = [json.loads(r["result"]) for r in self.db.rows("SELECT result FROM findings")]
        evidence += [m["content"] for m in self.db.rows("SELECT content FROM messages WHERE role = 'user'")]
        prior = len(self.db.rows("SELECT id FROM findings WHERE tool != 'remediation_verify'"))

        self._add_message("user", text)
        used = {"mode": self._llm_mode()}
        scripted = ScriptedModel()

        async def run_model(messages, tools):
            if used["mode"] != "workers-ai":
                return await scripted(messages, tools)
            try:
                return await self._workers_ai(messages, tools)
            except Exception as exc:
                if QUOTA_ERROR not in str(exc):
                    raise
                # ScriptedModel reads only the messages, so it can take over mid-turn.
                self._audit("model_fallback", {"error": str(exc)[:300], "to": "scripted"})
                used["mode"] = "scripted-fallback"
                return await scripted(messages, tools)

        copilot = Copilot(run_model, lambda name, args: self._execute_tool(inc["sid"], name, args), self._audit)
        result = await copilot.run_turn(history, text, evidence, prior)

        meta = {
            "number_check": result["number_check"],
            "proposal_id": result["proposal_id"],
            "tool_calls": result["tool_calls"],
            "llm_mode": used["mode"],
        }
        self._add_message("assistant", result["text"], meta)
        return {"role": "assistant", "content": result["text"], "meta": meta}

    async def _workers_ai(self, messages, tools):
        payload = {"messages": messages, "max_tokens": 1024, "temperature": 0.2}
        if tools:
            payload["tools"] = tools
        resp = await self.env.AI.run(MODEL, payload)
        if hasattr(resp, "to_py"):
            resp = resp.to_py()
        return json.loads(json.dumps(resp, default=str))

    async def _execute_tool(self, sid, name, args):
        if name == "propose_remediation":
            return await self._propose(sid, args)
        result = json.loads(await self._service(sid).tool(json.dumps({"name": name, "args": args})))
        self.db.run(
            "INSERT INTO findings (tool, args, result, created_at) VALUES (?, ?, ?, ?)",
            name, json.dumps(args), json.dumps(result), time.time(),
        )
        return result

    # -- proposals ----------------------------------------------------------

    async def _propose(self, sid, args):
        marks = ",".join("?" * len(OPEN_STATUSES))
        open_row = self.db.one(f"SELECT id, status FROM proposals WHERE status IN ({marks})", *OPEN_STATUSES)
        if open_row:
            return {"error": f"proposal {open_row['id']} is already open ({open_row['status']}); wait for a decision"}
        service = self._service(sid)
        err = json.loads(await service.validate(json.dumps(args)))["error"]
        if err:
            return {"error": err}
        snap = json.loads(await service.snapshot())

        inc = self._incident()
        pid = "prop-" + secrets.token_hex(4)
        now = time.time()
        params = {
            "incident_id": inc["id"],
            "sid": sid,
            "proposal_id": pid,
            "action": args["action"],
            "target_version": args.get("target_version", "") or "",
            "pool_size": args.get("pool_size", 0) or 0,
            "expected_version": snap["version"],
            "expected_fault": snap["fault"],
        }
        self.db.run(
            "INSERT INTO proposals (id, action, params, reason, fingerprint, status, workflow_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'creating', ?, ?, ?)",
            pid, args["action"], json.dumps(params), args.get("reason", ""), json.dumps(snap["fingerprint"]), pid, now, now,
        )
        try:
            await self.env.REMEDIATION.create({"id": pid, "params": params})
        except Exception as exc:
            self.db.run("DELETE FROM proposals WHERE id = ?", pid)
            self._audit("tool_error", {"tool": "propose_remediation", "error": f"workflow create failed: {exc}"})
            return {"error": "could not start the remediation workflow; nothing was changed"}
        self._audit("proposal", {"proposal_id": pid, "action": ops.describe(params), "reason": args.get("reason", "")})
        return {"proposal_id": pid, "status": "pending_approval", "action": ops.describe(params)}

    async def decide(self, payload):
        p = json.loads(payload)
        if not self._owns(p["sid"]):
            return json.dumps({"error": "not found"})
        row = self._proposal(p["proposal_id"])
        if not row:
            return json.dumps({"error": "not found"})
        if row["status"] != "awaiting_approval":
            return json.dumps({"error": "not awaiting approval", "status": row["status"]})

        approved = bool(p["approved"])
        # Flip status before any await: a second click sees the new status.
        new_status = "approving" if approved else "rejected"
        self.db.run("UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?", new_status, time.time(), row["id"])
        self._audit("approval" if approved else "rejection", {"proposal_id": row["id"], "action": ops.describe(row["params"])})
        try:
            instance = await self.env.REMEDIATION.get(row["workflow_id"])
            await instance.send_event({"type": "approval", "payload": {"approved": approved}})
        except Exception as exc:
            self.db.run("UPDATE proposals SET status = 'awaiting_approval' WHERE id = ?", row["id"])
            self._audit("workflow_error", {"proposal_id": row["id"], "error": str(exc)[:300]})
            return json.dumps({"error": "could not reach the workflow; try again"})
        await self._set_status("mitigating" if approved else "open")
        if not approved:
            self._notice(NOTICES["rejected"], {"proposal_id": row["id"]})
        return json.dumps({"status": new_status})

    async def proposal_status(self, payload):
        row = self._proposal(json.loads(payload)["proposal_id"])
        return json.dumps({"status": row["status"] if row else "missing"})

    async def workflow_update(self, payload):
        """Called from RemediationWorkflow steps. Idempotent per (proposal_id, step)."""
        p = json.loads(payload)
        pid, step, status = p["proposal_id"], p["step"], p["status"]
        if self.db.one("SELECT step FROM workflow_steps WHERE proposal_id = ? AND step = ?", pid, step):
            return json.dumps({"ok": True, "duplicate": True})
        self.db.run("INSERT INTO workflow_steps (proposal_id, step, at) VALUES (?, ?, ?)", pid, step, time.time())
        row = self._proposal(pid)
        if not row:
            return json.dumps({"error": "unknown proposal"})
        detail = p.get("detail", {})
        self._audit("workflow_step", {"proposal_id": pid, "step": step, "status": status, "detail": detail})

        if row["status"] not in ops.TERMINAL:
            self.db.run("UPDATE proposals SET status = ?, updated_at = ? WHERE id = ?", status, time.time(), pid)
        elif status != row["status"]:
            return json.dumps({"ok": True, "ignored": f"proposal already {row['status']}"})

        metrics = detail.get("metrics", {})
        fill = {"applied": ops.describe(row["params"]), "detail": detail.get("detail", ""), **metrics}
        if status == "succeeded":
            self.db.run(
                "INSERT INTO findings (tool, args, result, created_at) VALUES ('remediation_verify', '{}', ?, ?)",
                json.dumps(metrics), time.time(),
            )
            await self._set_status("resolved")
            inc = self._incident()
            await self._service(inc["sid"]).remember(json.dumps({
                "incident_id": inc["id"],
                "title": inc["title"],
                "tags": row["fingerprint"],
                "root_cause": row["reason"],
                "remediation": ops.describe(row["params"]),
                "resolved_at": time.time(),
            }))
            self._notice(NOTICES["succeeded"].format(**fill), {"proposal_id": pid})
        elif status in ("failed", "stale", "expired"):
            await self._set_status("open")
            self._notice(NOTICES[status].format(**{"error_rate_pct": "?", "p95_latency_ms": "?", **fill}), {"proposal_id": pid})
        return json.dumps({"ok": True})
