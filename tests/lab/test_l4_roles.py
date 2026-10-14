from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.agents.merger import merge_evidence
from src.lab.agents.orchestrator import MultiAgentOrchestrator
from src.lab.agents.roles import classify, read_chart, resolve_context
from src.lab.domain import DecisionKind, RoleOutput, RunBudget, RunStatus, TraceSummary
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.runtime.store import RunStore


def _env(task_id: str, run_id: str):
    tmp = tempfile.TemporaryDirectory()
    store = RunStore(Path(tmp.name) / "run.sqlite")
    task, obs = load_agent_view(task_id)
    env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id=run_id)
    return store, tmp, task, env


def _score(task, result):
    _, _, spec = load_for_eval(task.task_id)
    return verify_plan_decision(
        task_id=task.task_id,
        run_id=result.run_id,
        eval_spec=spec,
        decision=result.decision,
        final_snapshot=result.final_snapshot,
        trace=TraceSummary(tool_calls=result.usage.tool_calls),
    )


class ResolverHeuristicTests(unittest.TestCase):
    def test_three_fixtures(self):
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        out = resolve_context(obs)
        self.assertEqual(out.fields["kind"]["value"], "update")
        self.assertEqual(out.fields["referenced_plan_id"]["value"], "plan_a")
        self.assertEqual(classify(obs).fields["intent"]["value"], "update_stop_to_cost")

        _, closed = load_agent_view("tl_f0_closed_plan")
        self.assertEqual(resolve_context(closed).fields["kind"]["value"], "ignore")

        _, amb = load_agent_view("tl_f0_ambiguous_eth")
        amb_out = resolve_context(amb)
        self.assertEqual(amb_out.fields["kind"]["value"], "review")
        self.assertEqual(len(amb_out.conflicts), 2)


class MergerConflictTests(unittest.TestCase):
    def test_plan_id_disagreement_is_review_not_a_vote(self):
        resolver = RoleOutput(
            role="context_resolver",
            status="ok",
            fields={
                "kind": {"value": "update", "source": "r"},
                "referenced_plan_id": {"value": "plan_a", "source": "r"},
                "proposed_stop_loss": {"value": 1800, "source": "r"},
            },
            evidence_refs=("message:C",),
        )
        chart = RoleOutput(
            role="chart_agent",
            status="ok",
            fields={"plan_id": {"value": "plan_btc", "source": "chart_injected"}},
        )
        merged = merge_evidence({"context_resolver": resolver, "chart_agent": chart})
        self.assertEqual(merged.kind, DecisionKind.REVIEW)
        self.assertIsNone(merged.referenced_plan_id)
        self.assertTrue(any("plan_id" in c for c in merged.conflicts))


class OrchestratorScoreTests(unittest.TestCase):
    def _run(self, task_id: str, mode: str, **kwargs):
        store, tmp, task, env = _env(task_id, f"{mode}_{task_id}")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        result = MultiAgentOrchestrator(environment=env, mode=mode, **kwargs).run(
            task, run_id=f"{mode}_{task_id}"
        )
        return task, env, result

    def test_serial_and_concurrent_match_on_three_tasks(self):
        for task_id in (
            "tl_f0_move_sl_plan_a",
            "tl_f0_closed_plan",
            "tl_f0_ambiguous_eth",
        ):
            task, _, serial = self._run(task_id, "serial")
            _, _, conc = self._run(task_id, "concurrent")
            _, _, single = self._run(task_id, "single")
            for result in (serial, conc, single):
                self.assertEqual(result.status, RunStatus.SUCCEEDED)
                self.assertEqual(result.final_snapshot.submit_count, 1)
            self.assertEqual(serial.decision.kind, conc.decision.kind)
            self.assertEqual(serial.decision.referenced_plan_id, conc.decision.referenced_plan_id)
            self.assertTrue(_score(task, serial).hard_pass)
            self.assertTrue(_score(task, conc).hard_pass)
            self.assertTrue(_score(task, single).hard_pass)

    def test_unique_update_leaves_btc(self):
        _, _, result = self._run("tl_f0_move_sl_plan_a", "concurrent")
        plans = {p.plan_id: p for p in result.final_snapshot.plans}
        self.assertEqual(plans["plan_a"].stop_loss, 1800)
        self.assertEqual(plans["plan_btc"].stop_loss, 100000)
        merged = next(e["payload"] for e in result.events if e["kind"] == "evidence_merged")
        self.assertNotIn("market_context", merged["missing_fields"])

    def test_slow_fan_out_does_not_double_submit(self):
        _, env, serial = self._run(
            "tl_f0_move_sl_plan_a",
            "serial",
            chart_delay_sec=0.12,
            market_delay_sec=0.12,
        )
        _, _, conc = self._run(
            "tl_f0_move_sl_plan_a",
            "concurrent",
            chart_delay_sec=0.12,
            market_delay_sec=0.12,
        )
        self.assertEqual(serial.final_snapshot.submit_count, 1)
        self.assertEqual(conc.final_snapshot.submit_count, 1)
        serial_ms = next(e["payload"]["latency_ms"] for e in serial.events if e["kind"] == "run_finished")
        conc_ms = next(e["payload"]["latency_ms"] for e in conc.events if e["kind"] == "run_finished")
        self.assertGreaterEqual(serial_ms, 200)
        self.assertLess(conc_ms, serial_ms)
        self.assertTrue(any(e["kind"] == "evidence_merged" for e in conc.events))

    def test_failed_market_marks_missing_and_still_commits(self):
        task, env, result = self._run("tl_f0_move_sl_plan_a", "concurrent", market_fail=True)
        merged = next(e["payload"] for e in result.events if e["kind"] == "evidence_merged")
        self.assertIn("market_context", merged["missing_fields"])
        self.assertTrue(_score(task, result).hard_pass)
        self.assertEqual(env.gateway.submit_count, 1)

    def test_injected_chart_conflict_reviews_without_updating(self):
        _, env, result = self._run(
            "tl_f0_move_sl_plan_a",
            "serial",
            chart_forced_plan_id="plan_btc",
        )
        self.assertEqual(result.decision.kind, DecisionKind.REVIEW)
        plan_a = next(p for p in result.final_snapshot.plans if p.plan_id == "plan_a")
        self.assertEqual(plan_a.stop_loss, 1650)
        self.assertEqual(result.final_snapshot.submit_count, 1)

    def test_shared_budget_blocks_commit_after_fan_out(self):
        store, tmp, task, env = _env("tl_f0_move_sl_plan_a", "budget")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        result = MultiAgentOrchestrator(
            environment=env,
            mode="concurrent",
            budget=RunBudget(max_model_calls=4, max_tool_calls=2, max_mutates=1),
        ).run(task, run_id="budget")
        self.assertEqual(result.status, RunStatus.TIMED_OUT)
        self.assertEqual(result.final_snapshot.submit_count, 0)
        self.assertIn("budget_exceeded", result.terminal_reason)

    def test_roles_do_not_share_a_mutable_signal(self):
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        a = resolve_context(obs)
        b = read_chart(obs, symbol="ETH")
        self.assertIsNot(a.fields, b.fields)
        a.fields["injected"] = {"value": "nope", "source": "test"}
        self.assertNotIn("injected", b.fields)


if __name__ == "__main__":
    unittest.main()
