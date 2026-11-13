import unittest

import ops
from tests.fakes import NOW, FakeService


def rollback(svc, target="v2.14.1"):
    return {"proposal_id": "prop-00000001", "action": "rollback_deploy", "target_version": target, "pool_size": 0,
            "expected_version": svc.state["version"], "expected_fault": svc.state["fault"]}


class OpsTest(unittest.TestCase):
    def test_rollback_target_rules(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        self.assertEqual(ops.validate_remediation(rollback(svc), svc.state, svc.deploys), "")
        self.assertIn("unknown version", ops.validate_remediation(rollback(svc, "v9.9.9"), svc.state, svc.deploys))
        self.assertIn("already running", ops.validate_remediation(rollback(svc, "v2.15.0"), svc.state, svc.deploys))

    def test_apply_is_compare_and_set(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        p = rollback(svc)
        svc.state["fault"] = "none"  # someone clicked Reset after the proposal
        new, result = ops.apply(p, svc.state, svc.deploys, NOW)
        self.assertIsNone(new)
        self.assertEqual(result["error"], "stale")

    def test_apply_then_settle(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        new, result = ops.apply(rollback(svc), svc.state, svc.deploys, NOW)
        self.assertTrue(result["ok"])
        self.assertEqual(new["version"], "v2.14.1")
        self.assertEqual(ops.settle(new, NOW + 5)["fault"], "bad_deploy")
        self.assertEqual(ops.settle(new, NOW + 25)["fault"], "none")


if __name__ == "__main__":
    unittest.main()
