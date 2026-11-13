import unittest

import guardrails as g
from tools import TOOLS_BY_NAME

METRICS = TOOLS_BY_NAME["get_metrics"]["parameters"]
PROPOSE = TOOLS_BY_NAME["propose_remediation"]["parameters"]


class ValidateTest(unittest.TestCase):
    def test_string_int_and_default(self):
        self.assertEqual(
            g.validate_args(METRICS, {"metric": "rps", "window_minutes": "30"}),
            {"metric": "rps", "window_minutes": 30},
        )
        self.assertEqual(g.validate_args(METRICS, '{"metric": "rps"}')["window_minutes"], 15)

    def test_rejections(self):
        cases = [
            {"metric": "cpu"},  # enum
            {"metric": "rps", "window_minutes": 600},  # range
            {"metric": "rps", "window_minutes": True},  # bool is not int
            {},  # required
        ]
        for args in cases:
            with self.assertRaises(g.ToolArgError, msg=args):
                g.validate_args(METRICS, args)
        with self.assertRaises(g.ToolArgError):
            g.validate_args(METRICS, "not json")

    def test_unknown_keys_dropped(self):
        out = g.validate_args(PROPOSE, {"action": "failover_payment_provider", "reason": "x", "force": True})
        self.assertNotIn("force", out)


RESULT = {
    "metric": "error_rate_pct",
    "current": 23.84,
    "baseline": 0.41,
    "ratio_to_baseline": 58.1,
    "points": [{"minute": "2026-11-12T10:05:00Z", "value": 23.8}],
    "lines": [{"ts": "2026-11-12T10:05:10Z", "msg": "Timeout acquiring DB connection after 30000ms (pool 50/50 in use)"}],
}


class NumberCheckTest(unittest.TestCase):
    def check(self, text, sources=(RESULT,)):
        return g.check_numbers(text, list(sources))

    def test_grounded_and_rounded(self):
        r = self.check("Error rate is 23.8% (about 24%) vs a 0.4% baseline, 58.1x higher.")
        self.assertTrue(r["ok"], r)

    def test_invented_number(self):
        self.assertEqual(self.check("Error rate is 40% right now.")["unverified"], ["40"])

    def test_skips_versions_times_codes_percentiles(self):
        text = "Since v2.15.0 at 10:05 on 2026-11-12, HTTP 500s rose; p95 and p99 are up; 100% of the pool."
        self.assertTrue(self.check(text)["ok"], self.check(text))

    def test_numbers_in_log_lines_allowed(self):
        self.assertTrue(self.check("Requests wait 30000ms for a connection; pool is 50/50.")["ok"])

    def test_precision_is_decimal_places_only(self):
        self.assertFalse(self.check("p95 is 2600ms", [{"current": 2643}])["ok"])

    def test_tool_args_and_model_text_are_not_evidence(self):
        sources = [{"root_cause": "pool hit 40 connections", "title": "40 errors", "resolved_at": 1800000040}]
        self.assertFalse(self.check("Error rate is 40%.", sources)["ok"])

    def test_redaction(self):
        self.assertEqual(g.redact_numbers("Error rate 40% vs 0.4%", ["40"]), "Error rate [unverified]% vs 0.4%")


if __name__ == "__main__":
    unittest.main()


class ShowsProblemTest(unittest.TestCase):
    def test_elevated_metric_or_error_logs(self):
        self.assertTrue(g.shows_problem([{"metric": "p95_latency_ms", "ratio_to_baseline": 19.6}]))
        self.assertTrue(g.shows_problem([{"lines": [{"level": "WARN", "msg": "gateway slow"}]}]))

    def test_healthy_evidence(self):
        healthy = [
            {"metric": "error_rate_pct", "ratio_to_baseline": 1.0},
            {"total_matching": 0, "lines": []},
            {"lines": [{"level": "INFO", "msg": "HTTP 200 POST /checkout"}]},
            "checkout is throwing 500s",  # user text is not evidence
        ]
        self.assertFalse(g.shows_problem(healthy))
        self.assertFalse(g.shows_problem([]))
