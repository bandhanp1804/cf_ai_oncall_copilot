import json
import unittest

import copilot
import ops
from copilot import Copilot, parse_model_response
from scripted import ScriptedModel
from tests.fakes import NOW, Audit, FakeService
from tools import TOOLS_BY_NAME


class Script:
    """A model that returns canned responses in order and records what it saw."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.seen = []

    async def __call__(self, messages, tools):
        self.seen.append((messages, tools))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def call(name, **args):
    return {"name": name, "arguments": args}


class ScriptedTurnTest(unittest.IsolatedAsyncioTestCase):
    async def test_bad_deploy_turn_is_grounded_and_proposes_rollback(self):
        svc, audit = FakeService(), Audit()
        svc.trigger("bad_deploy")
        out = await Copilot(ScriptedModel(), svc.execute_tool, audit).run_turn([], "checkout is throwing 500s, why?", [])
        self.assertIn("v2.15.0", out["text"])
        self.assertTrue(out["number_check"]["ok"], out)
        self.assertTrue(out["proposal_id"])
        self.assertEqual(svc.proposals[0]["target_version"], "v2.14.1")
        self.assertIn("tool_call", audit.kinds())

    async def test_second_incident_recalls_past_fix(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        import memory

        svc.past.append({"incident_id": "inc-0000beef", "title": "checkout 500s", "root_cause": "bad deploy",
                         "remediation": "rollback_deploy to v2.14.1", "resolved_at": NOW - 3600,
                         "tags": memory.fingerprint(svc.state, svc.deploys, NOW)})
        out = await Copilot(ScriptedModel(), svc.execute_tool, Audit()).run_turn([], "checkout failing again", [])
        self.assertIn("you fixed it before with rollback_deploy", out["text"])
        self.assertTrue(out["number_check"]["ok"], out)

    async def test_healthy_service_proposes_nothing(self):
        svc = FakeService()
        out = await Copilot(ScriptedModel(), svc.execute_tool, Audit()).run_turn([], "is checkout ok?", [])
        self.assertIn("healthy", out["text"])
        self.assertEqual(out["proposal_id"], "")


class RobustnessTest(unittest.IsolatedAsyncioTestCase):
    async def test_bad_args_retry_once_then_stop(self):
        svc, audit = FakeService(), Audit()
        bad = {"response": None, "tool_calls": [call("get_metrics", metric="cpu")]}
        model = Script(bad, bad, {"response": "I could not read metrics."})
        out = await Copilot(model, svc.execute_tool, audit).run_turn([], "why?", [])
        self.assertEqual(out["text"], "I could not read metrics.")
        self.assertIsNone(model.seen[-1][1], "final answer must be requested without tools")
        self.assertIn("stopping tool loop", json.dumps(audit.entries))
        tool_msgs = [m for m in model.seen[1][0] if m["role"] == "tool"]
        self.assertIn("must be one of", tool_msgs[0]["content"])

    async def test_tool_call_written_as_text(self):
        svc = FakeService()
        text_call = {"response": '<|python_tag|>{"name": "get_recent_deploys", "parameters": {"limit": "3"}}'}
        model = Script(text_call, {"response": "Running v2.14.1."})
        out = await Copilot(model, svc.execute_tool, Audit()).run_turn([], "what is deployed?", [])
        self.assertEqual(out["tool_calls"], 1)
        self.assertEqual(out["text"], "Running v2.14.1.")

    def test_prose_with_json_is_not_a_call(self):
        calls, text = parse_model_response({"response": 'Use {"name": "get_metrics"} next'})
        self.assertEqual(calls, [])
        calls, _ = parse_model_response({"response": '{"name": "drop_database", "parameters": {}}'})
        self.assertEqual(calls, [])

    def test_openai_shape(self):
        resp = {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "search_logs", "arguments": '{"level": "ERROR"}'}}]}}]}
        calls, _ = parse_model_response(resp)
        self.assertEqual(calls[0]["name"], "search_logs")

    async def test_invented_number_rewritten(self):
        svc, audit = FakeService(), Audit()
        model = Script(
            {"response": None, "tool_calls": [call("get_metrics", metric="error_rate_pct")]},
            {"response": "Error rate is 40%."},
            {"response": "Error rate is near baseline."},
        )
        out = await Copilot(model, svc.execute_tool, audit).run_turn([], "error rate?", [])
        self.assertEqual(out["text"], "Error rate is near baseline.")
        self.assertTrue(out["number_check"]["rewritten"])

    async def test_invented_number_redacted_when_rewrite_fails(self):
        svc, audit = FakeService(), Audit()
        model = Script(
            {"response": None, "tool_calls": [call("get_metrics", metric="error_rate_pct")]},
            {"response": "Error rate is 40%."},
            {"response": "Error rate is still 40%."},
        )
        out = await Copilot(model, svc.execute_tool, audit).run_turn([], "error rate?", [])
        self.assertEqual(out["text"], "Error rate is still [unverified]%.")
        self.assertTrue(out["number_check"]["redacted"])
        self.assertIn("Error rate is still 40%.", json.dumps(audit.entries), "raw draft kept in audit")

    async def test_model_outage(self):
        audit = Audit()
        out = await Copilot(Script(RuntimeError("3040: capacity")), FakeService().execute_tool, audit).run_turn([], "hi", [])
        self.assertEqual(out["text"], copilot.MODEL_UNAVAILABLE)
        self.assertIn("model_error", audit.kinds())

    async def test_propose_requires_evidence(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        model = Script(
            {"response": None, "tool_calls": [call("propose_remediation", action="rollback_deploy",
                                                   target_version="v2.14.1", reason="guess")]},
            {"response": "I need to investigate first."},
        )
        out = await Copilot(model, svc.execute_tool, Audit()).run_turn([], "fix it", [])
        self.assertEqual(svc.proposals, [])
        self.assertEqual(out["proposal_id"], "")

    async def test_too_many_calls_in_one_step(self):
        svc = FakeService()
        many = {"response": None, "tool_calls": [call("get_recent_deploys")] * 6}
        model = Script(many, {"response": "Done."})
        out = await Copilot(model, svc.execute_tool, Audit()).run_turn([], "deploys?", [])
        self.assertEqual(out["tool_calls"], 4)

    def test_model_cannot_reach_write_paths(self):
        self.assertFalse({"apply_remediation", "set_fault", "reset", "remember"} & set(TOOLS_BY_NAME))


if __name__ == "__main__":
    unittest.main()
