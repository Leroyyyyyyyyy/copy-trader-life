from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.domain import DecisionKind, SubmittedDecision, TraceSummary
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.experiments.real_replay import TASKS, rule_parser_pass, rule_parser_view
from src.lab.harness.loop import AgentHarness
from src.lab.models.stub import StubModelAdapter
from src.lab.runtime.store import RunStore


def _order(**args):
    base = {"symbol": "BTC", "side": "sell", "stop_loss": 84000, "evidence_refs": ["message:S1"],
            "expected_state_version": 2}
    base.update(args)
    return [{"tool_calls": [{"name": "submit_new_order", "arguments": base}]}, {"final": {"done": True}}]


class RealReplayTests(unittest.TestCase):
    def _run(self, task_id: str, script, allow_open: bool = True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = RunStore(Path(tmp.name) / "r.sqlite")
        self.addCleanup(store.close)
        task, obs = load_agent_view(task_id)
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="r", allow_open=allow_open)
        result = AgentHarness(store=store, model=StubModelAdapter(script), environment=env).run(task, run_id="r")
        _, _, spec = load_for_eval(task_id)
        score = verify_plan_decision(task_id=task_id, run_id="r", eval_spec=spec,
                                     decision=result.decision or SubmittedDecision(kind=DecisionKind.IGNORE),
                                     final_snapshot=result.final_snapshot, trace=TraceSummary())
        return result, score

    def test_rule_parser_reproduces_replay(self):
        # These are the actions the paper engine took on 2026-10-14.
        views = {t: rule_parser_view(t) for t in TASKS}
        self.assertEqual((views["tl_real_8089_negation"]["side"], views["tl_real_8089_negation"]["intent"]), ("sell", "market"))
        self.assertEqual((views["tl_real_8092_complaint"]["side"], views["tl_real_8092_complaint"]["symbol"]), ("buy", "ETH"))
        self.assertEqual(views["tl_real_8081_recap"]["take_profit"], 2700.0)
        self.assertEqual([rule_parser_pass(t, views[t]) for t in TASKS], [False, False, False, True])

    def test_open_with_stop_passes_control(self):
        result, score = self._run("tl_synth_explicit_short", _order(take_profit=78000))
        self.assertTrue(score.hard_pass, score)
        self.assertEqual(result.final_snapshot.submit_count, 1)

    def test_open_without_stop_is_rejected(self):
        script = [{"tool_calls": [{"name": "submit_new_order", "arguments": {
            "symbol": "BTC", "side": "sell", "stop_loss": None, "evidence_refs": ["message:S1"]}}]}, {"final": {}}]
        result, score = self._run("tl_synth_explicit_short", script)
        codes = [e["payload"].get("error_code") for e in result.events if e["kind"] == "tool_result"]
        self.assertTrue(any("missing_stop_loss" in (c or "") for c in codes), codes)
        self.assertEqual(result.final_snapshot.submit_count, 0)
        self.assertFalse(score.hard_pass)

    def test_unwanted_open_is_flagged(self):
        result, score = self._run("tl_real_8089_negation", _order())
        self.assertIn("unexpected_new_plan:BTC:sell", score.semantic_errors)
        self.assertFalse(score.hard_pass)

    def test_open_tool_is_opt_in(self):
        result, _ = self._run("tl_synth_explicit_short", _order(), allow_open=False)
        codes = [e["payload"].get("error_code") for e in result.events if e["kind"] == "tool_result"]
        self.assertEqual(codes[0], "unknown_tool")


if __name__ == "__main__":
    unittest.main()
