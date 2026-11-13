import unittest

import sim
from tests.fakes import NOW


class SimTest(unittest.TestCase):
    def test_baseline_in_range(self):
        state = sim.default_state()
        for metric, (low, high) in sim.RANGES["none"].items():
            self.assertTrue(low <= sim.metric_value(state, metric, NOW) <= high, metric)

    def test_bad_deploy_full_effect_after_ramp(self):
        state = {**sim.default_state(), "fault": "bad_deploy", "fault_started_at": NOW - 120}
        err = sim.metric_value(state, "error_rate_pct", NOW)
        self.assertTrue(22 <= err <= 26, err)

    def test_values_deterministic_within_minute(self):
        state = {**sim.default_state(), "fault": "latency_spike", "fault_started_at": NOW - 300}
        a = sim.metric_series(state, "p95_latency_ms", 15, NOW)
        b = sim.metric_series(state, "p95_latency_ms", 15, NOW)
        self.assertEqual(a, b)

    def test_matching_fix_decays_to_baseline(self):
        state = {
            **sim.default_state(),
            "fault": "bad_deploy",
            "fault_started_at": NOW - 300,
            "remediated_at": NOW - 10,
            "remediation": "rollback_deploy",
        }
        self.assertAlmostEqual(sim.fault_intensity(state, NOW), 0.5, places=2)
        self.assertFalse(sim.is_recovered(state, NOW))
        self.assertTrue(sim.is_recovered(state, NOW + 10))
        self.assertLess(sim.metric_value(state, "error_rate_pct", NOW + 10), 1.0)

    def test_wrong_fix_does_not_decay(self):
        state = {
            **sim.default_state(),
            "fault": "db_pool_exhaustion",
            "fault_started_at": NOW - 300,
            "remediated_at": NOW - 60,
            "remediation": "rollback_deploy",
        }
        self.assertEqual(sim.fault_intensity(state, NOW), 1.0)
        self.assertFalse(sim.is_recovered(state, NOW))

    def test_fault_log_signatures(self):
        state = {**sim.default_state(), "fault": "db_pool_exhaustion", "fault_started_at": NOW - 600}
        logs = sim.search_logs(state, NOW, query="DB connection", limit=50)
        self.assertGreater(logs["total_matching"], 0)
        self.assertEqual(sim.top_error_signature(state, NOW), "db_pool_timeout")

    def test_pool_metrics_have_derived_fields(self):
        state = {**sim.default_state(), "fault": "db_pool_exhaustion", "fault_started_at": NOW - 600}
        m = sim.metric_series(state, "db_pool_in_use", 15, NOW)
        self.assertEqual(m["current"], 50)
        self.assertEqual(m["pool_utilization_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
