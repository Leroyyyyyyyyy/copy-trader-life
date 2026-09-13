from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.domain import (
    DecisionKind,
    EnvironmentKind,
    RunBudget,
    RunStatus,
    SubmittedDecision,
    TraceSummary,
)
from src.lab.environments.message_analysis import MessageAnalysisEnvironment
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.models.stub import StubModelAdapter
from src.lab.runtime.store import RunStore


def _store() -> tuple[RunStore, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    store = RunStore(Path(tmp.name) / "run.sqlite")
    return store, tmp


class PlanSimulationHarnessTests(unittest.TestCase):
    def test_success_updates_plan_and_scores(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        self.assertEqual(task.environment_kind, EnvironmentKind.PLAN_SIMULATION)
        gateway = SimulationGateway(store)
        env = PlanSimulationEnvironment(obs, gateway, run_id="run_plan_ok")
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "get_account_snapshot", "arguments": {}}]},
                {
                    "tool_calls": [
                        {
                            "name": "submit_plan_update",
                            "arguments": {
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C", "plan:plan_a"],
                                "expected_state_version": 3,
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        result = AgentHarness(store=store, model=model, environment=env).run(
            task, run_id="run_plan_ok"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIsNotNone(result.decision)
        self.assertEqual(result.final_snapshot.submit_count, 1)
        plan_a = next(p for p in result.final_snapshot.plans if p.plan_id == "plan_a")
        self.assertEqual(plan_a.stop_loss, 1800)
        _, _, spec = load_for_eval(task.task_id)
        score = verify_plan_decision(
            task_id=task.task_id,
            run_id=result.run_id,
            eval_spec=spec,
            decision=result.decision,
            final_snapshot=result.final_snapshot,
            trace=TraceSummary(
                tool_calls=result.usage.tool_calls,
                claimed_done_without_submit=result.claimed_done_without_submit,
            ),
        )
        self.assertTrue(score.hard_pass)
        kinds = [e["kind"] for e in result.events]
        self.assertIn("tool_result", kinds)
        self.assertIn("run_finished", kinds)

    def test_unknown_tool_then_recover(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="run_recover")
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "not_a_tool", "arguments": {}}]},
                {
                    "tool_calls": [
                        {
                            "name": "submit_plan_update",
                            "arguments": {
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C"],
                                "expected_state_version": 3,
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        result = AgentHarness(store=store, model=model, environment=env).run(
            task, run_id="run_recover"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        tool_events = [e for e in result.events if e["kind"] == "tool_result"]
        self.assertEqual(tool_events[0]["payload"]["error_code"], "unknown_tool")
        self.assertEqual(result.final_snapshot.submit_count, 1)

    def test_budget_exhaustion(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="run_budget")
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "get_account_snapshot", "arguments": {}}]},
                {"tool_calls": [{"name": "get_account_snapshot", "arguments": {}}]},
                {"tool_calls": [{"name": "get_account_snapshot", "arguments": {}}]},
            ]
        )
        result = AgentHarness(
            store=store,
            model=model,
            environment=env,
            budget=RunBudget(max_model_calls=2, max_tool_calls=8, max_mutates=1),
        ).run(task, run_id="run_budget")
        self.assertEqual(result.status, RunStatus.TIMED_OUT)
        self.assertIn("budget_exceeded", result.terminal_reason)


class MessageAnalysisHarnessTests(unittest.TestCase):
    def test_same_loop_message_analysis_success(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f1_msg_ref_plan_a")
        self.assertEqual(task.environment_kind, EnvironmentKind.MESSAGE_ANALYSIS)
        env = MessageAnalysisEnvironment(obs, SimulationGateway(store), run_id="run_msg_ok")
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "list_messages", "arguments": {}}]},
                {
                    "tool_calls": [
                        {
                            "name": "submit_decision",
                            "arguments": {
                                "kind": "update",
                                "referenced_plan_id": "plan_a",
                                "evidence_refs": ["message:C", "message:A", "plan:plan_a"],
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        # Same AgentHarness class as plan simulation.
        result = AgentHarness(store=store, model=model, environment=env).run(
            task, run_id="run_msg_ok"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.decision.referenced_plan_id, "plan_a")
        plan_a = next(p for p in result.final_snapshot.plans if p.plan_id == "plan_a")
        self.assertEqual(plan_a.stop_loss, 1650)
        _, _, spec = load_for_eval(task.task_id)
        score = verify_plan_decision(
            task_id=task.task_id,
            run_id=result.run_id,
            eval_spec=spec,
            decision=result.decision,
            final_snapshot=result.final_snapshot,
        )
        self.assertTrue(score.hard_pass)


class IdempotentCommitTests(unittest.TestCase):
    def test_duplicate_commit_reuses_receipt(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        gateway = SimulationGateway(store)
        gateway.reset(obs.plans, state_version=3)
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C",),
            expected_state_version=3,
        )
        first = gateway.commit(run_id="run_idem", decision=decision)
        self.assertTrue(first.applied)
        self.assertFalse(first.reused)
        version_after = gateway.state_version
        second = gateway.commit(run_id="run_idem", decision=decision)
        self.assertTrue(second.reused)
        self.assertEqual(gateway.state_version, version_after)
        self.assertEqual(gateway.submit_count, 1)

    def test_same_key_different_payload_rejected(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        gateway = SimulationGateway(store)
        gateway.reset(obs.plans, state_version=3)
        d1 = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C",),
            expected_state_version=3,
        )
        d2 = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1810},
            evidence_refs=("message:C",),
            expected_state_version=3,
        )
        self.assertTrue(gateway.commit(run_id="run_mismatch", decision=d1).applied)
        bad = gateway.commit(run_id="run_mismatch", decision=d2)
        self.assertFalse(bad.applied)
        self.assertEqual(bad.error_code, "idempotency_payload_mismatch")


if __name__ == "__main__":
    unittest.main()
