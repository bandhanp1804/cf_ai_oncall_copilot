import unittest

import memory
from tests.fakes import NOW, FakeService


class MemoryTest(unittest.TestCase):
    def test_bad_deploy_fingerprint(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        tags = memory.fingerprint(svc.state, svc.deploys, NOW)
        self.assertEqual(tags, ["high_error_rate", "recent_deploy", "sig:npe_payment_client_v2"])

    def test_second_bad_deploy_matches_first(self):
        svc = FakeService()
        svc.trigger("bad_deploy")
        tags = memory.fingerprint(svc.state, svc.deploys, NOW)
        past = [{"incident_id": "inc-00000001", "title": "checkout 500s", "tags": tags, "root_cause": "bad deploy",
                 "remediation": "rollback_deploy to v2.14.1", "resolved_at": NOW - 3600}]
        matches = memory.find_similar(tags, past)
        self.assertEqual(matches[0]["score"], 1.0)

    def test_different_fault_does_not_match(self):
        svc = FakeService()
        svc.trigger("db_pool_exhaustion", ago=600)
        db_tags = memory.fingerprint(svc.state, svc.deploys, NOW)
        past = [{"incident_id": "inc-1", "title": "x", "tags": ["high_error_rate", "recent_deploy", "sig:npe_payment_client_v2"],
                 "root_cause": "", "remediation": "", "resolved_at": NOW}]
        self.assertEqual(memory.find_similar(db_tags, past), [])


if __name__ == "__main__":
    unittest.main()
