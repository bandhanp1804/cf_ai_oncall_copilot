"""Worker entrypoint: /api/* routes. Static assets (the Next.js export) are
served by the platform before this code runs for any other path."""

import json
import re
import secrets
from urllib.parse import urlparse

from workers import Response, WorkerEntrypoint

from agent import IncidentAgent  # noqa: F401  (Durable Object class export)
from remediation import RemediationWorkflow  # noqa: F401  (Workflow class export)
from service import CheckoutService  # noqa: F401  (Durable Object class export)

SID = re.compile(r"^[a-f0-9]{12}$")
INCIDENT = re.compile(r"^/api/incidents/(inc-[a-f0-9]{8})(?:/(chat|proposals/(prop-[a-f0-9]{8})/(approve|reject)))?$")
DEV_ORIGIN = "http://localhost:3000"
ERROR_STATUS = {"not found": 404, "busy": 409, "not awaiting approval": 409}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        origin = request.headers.get("Origin") or ""
        if request.method == "OPTIONS":
            return self._json({}, 204, origin)
        try:
            data, status = await self._route(request)
        except Exception as exc:  # never leak a stack trace as HTML
            data, status = {"error": f"internal error: {str(exc)[:200]}"}, 500
        return self._json(data, status, origin)

    @staticmethod
    def _json(data, status, origin):
        headers = {"content-type": "application/json", "cache-control": "no-store"}
        if origin == DEV_ORIGIN:
            headers.update({
                "access-control-allow-origin": DEV_ORIGIN,
                "access-control-allow-headers": "content-type, x-sandbox-id",
                "access-control-allow-methods": "GET, POST, OPTIONS",
            })
        return Response(json.dumps(data), status=status, headers=headers)

    async def _body(self, request):
        try:
            body = await request.json()
            return dict(body) if body else {}
        except Exception:
            return {}

    @staticmethod
    def _reply(raw):
        data = json.loads(raw)
        return data, ERROR_STATUS.get(data.get("error"), 400) if isinstance(data, dict) and data.get("error") else 200

    async def _route(self, request):
        path = urlparse(request.url).path.rstrip("/")
        method = request.method
        sid = (request.headers.get("X-Sandbox-Id") or "").lower()
        if not SID.match(sid):
            return {"error": "missing or invalid X-Sandbox-Id header"}, 400
        service = self.env.CHECKOUT_SERVICE.getByName("checkout-" + sid)

        if path == "/api/service" and method == "GET":
            return json.loads(await service.status()), 200
        if path == "/api/service/fault" and method == "POST":
            return self._reply(await service.set_fault(json.dumps({"fault": (await self._body(request)).get("fault", "")})))
        if path == "/api/service/reset" and method == "POST":
            return json.loads(await service.reset()), 200

        if path == "/api/incidents" and method == "GET":
            return {"incidents": json.loads(await service.list_incidents())}, 200
        if path == "/api/incidents" and method == "POST":
            body = await self._body(request)
            incident_id = "inc-" + secrets.token_hex(4)
            title = str(body.get("title") or "New incident")[:80]
            agent = self.env.INCIDENT_AGENT.getByName(incident_id)
            return json.loads(await agent.init(json.dumps({"id": incident_id, "sid": sid, "title": title}))), 200

        m = INCIDENT.match(path)
        if not m:
            return {"error": "not found"}, 404
        incident_id, sub, proposal_id, decision = m.groups()
        agent = self.env.INCIDENT_AGENT.getByName(incident_id)

        if sub is None and method == "GET":
            return self._reply(await agent.get_state(json.dumps({"sid": sid})))
        if sub == "chat" and method == "POST":
            message = str((await self._body(request)).get("message", "")).strip()
            if not message:
                return {"error": "message is required"}, 400
            if len(message) > 2000:
                return {"error": "message is too long (max 2000 characters)"}, 400
            outcome = await self.env.CHAT_LIMITER.limit({"key": sid})
            allowed = outcome["success"] if isinstance(outcome, dict) else getattr(outcome, "success", True)
            if not allowed:
                return {"error": "rate limited: at most 10 messages per minute"}, 429
            return self._reply(await agent.chat(json.dumps({"sid": sid, "message": message})))
        if decision and method == "POST":
            payload = {"sid": sid, "proposal_id": proposal_id, "approved": decision == "approve"}
            return self._reply(await agent.decide(json.dumps(payload)))
        return {"error": "not found"}, 404
