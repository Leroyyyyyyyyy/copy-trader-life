from __future__ import annotations

import unittest

from src.lab.experiments.e2e_cli import run_e2e


class CliEndToEndTests(unittest.TestCase):
    """Real CLI subprocesses on a fresh DB; deterministic stub agent."""

    def test_all_scenarios(self):
        report = run_e2e("stub")
        for sc in report["scenarios"]:
            with self.subTest(scenario=sc["scenario"]):
                failed = [c for c in sc["checks"] if not c["ok"]]
                self.assertEqual(failed, [])
                self.assertTrue(sc["score"]["hard_pass"], sc["score"])
        self.assertTrue(report["invariants_passed"])


if __name__ == "__main__":
    unittest.main()
