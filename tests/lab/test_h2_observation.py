from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.domain import ObservationPolicy, PlanStatus, RunBudget
from src.lab.environments.allowed_actions import allowed_actions_from_plans
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.execution.simulation import SimulationGateway
from src.lab.experiments.h2_observation import run_h2_matrix, run_one_case
from src.lab.harness.loop import AgentHarness
from src.lab.models.policy_stub import NaiveEthUpdateAdapter
from src.lab.runtime.store import RunStore


def _store() -> tuple[RunStore, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    return RunStore(Path(tmp.name) / "run.sqlite"), tmp


class AllowedActionsDerivationTests(unittest.TestCase):
    def test_closed_plan_update_is_illegal_without_using_labels(self):
        _, obs = load_agent_view("tl_f0_closed_plan")
        actions = allowed_actions_from_plans(obs.plans)
        by_plan = {
            a["plan_id"]: a
            for a in actions
            if a["tool"] == "submit_plan_update"
        }
        self.assertFalse(by_plan["plan_a"]["legal"])
        self.assertEqual(by_plan["plan_a"]["reason"], "plan_not_filled:closed")
        self.assertTrue(by_plan["plan_btc"]["legal"])
        kinds = {a["kind"] for a in actions if a["tool"] == "submit_decision"}
        self.assertEqual(kinds, {"ignore", "review"})
        blob = str(actions)
        self.assertNotIn("acceptable_outcomes", blob)
        self.assertNotIn("gold", blob)

    def test_ambiguous_lists_both_filled_updates(self):
        _, obs = load_agent_view("tl_f0_ambiguous_eth")
        _, _, spec = load_for_eval("tl_f0_ambiguous_eth")
        self.assertEqual(spec.acceptable_outcomes[0].kind.value, "review")
        actions = allowed_actions_from_plans(obs.plans)
        updates = [a for a in actions if a["tool"] == "submit_plan_update"]
        self.assertEqual({a["plan_id"] for a in updates}, {"plan_eth_1", "plan_eth_2"})
        self.assertTrue(all(a["legal"] for a in updates))

    def test_unique_task_does_not_hide_other_filled_symbol(self):
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        actions = allowed_actions_from_plans(obs.plans)
        updates = {
            a["plan_id"]: a for a in actions if a["tool"] == "submit_plan_update"
        }
        self.assertTrue(updates["plan_a"]["legal"])
        self.assertTrue(updates["plan_btc"]["legal"])
        self.assertEqual(obs.plans[0].status, PlanStatus.FILLED)


class ObservationPolicyPromptTests(unittest.TestCase):
    def _prompt(self, policy: ObservationPolicy) -> dict:
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(
            obs, SimulationGateway(store), run_id="p", observation_policy=policy
        )
        model = NaiveEthUpdateAdapter()
        AgentHarness(
            store=store,
            model=model,
            environment=env,
            budget=RunBudget(max_model_calls=4, max_tool_calls=8, max_mutates=1),
            skill_mode="none",
        ).run(task, run_id="p")
        return model.requests[0]

    def test_state_only_omits_allowed_actions(self):
        req = self._prompt(ObservationPolicy.STATE_ONLY)
        self.assertNotIn("allowed_actions", req)
        self.assertEqual(req["observation_policy"], "state_only")
        self.assertEqual(req["skill_catalog"], [])
        self.assertEqual(req["memories"], [])
        self.assertEqual(req["history"]["policy"], "full")

    def test_allowed_actions_present_and_tools_match(self):
        state = self._prompt(ObservationPolicy.STATE_ONLY)
        listed = self._prompt(ObservationPolicy.ALLOWED_ACTIONS)
        self.assertIn("allowed_actions", listed)
        self.assertEqual(
            {t["name"] for t in state["tools"]},
            {t["name"] for t in listed["tools"]},
        )
        self.assertIn("submit_plan_update", {t["name"] for t in state["tools"]})
        self.assertIn("submit_decision", {t["name"] for t in state["tools"]})
        self.assertGreater(
            listed["manifest"]["active_tokens"],
            state["manifest"]["active_tokens"],
        )


class H2MatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_h2_matrix()

    def test_four_cells_and_frozen_controls(self):
        self.assertEqual(set(self.report["cells"]), {"A0", "A1", "B0", "B1"})
        self.assertEqual(self.report["model_kind"], "stub")
        for cell in self.report["cells"].values():
            for case in cell["per_case"]:
                self.assertEqual(case["prompt"]["skill_catalog"], [])
                self.assertEqual(case["prompt"]["memories"], [])
                self.assertEqual(case["prompt"]["history_policy"], "full")
                names = case["prompt"]["tool_names"]
                self.assertEqual(names, self.report["cells"]["A0"]["per_case"][0]["prompt"]["tool_names"])

    def test_naive_a_helped_on_closed_not_on_ambiguous(self):
        def row(cell: str, task_id: str) -> dict:
            return next(
                c for c in self.report["cells"][cell]["per_case"] if c["task_id"] == task_id
            )

        self.assertTrue(row("A0", "tl_f0_move_sl_plan_a")["hard_pass"])
        self.assertFalse(row("A0", "tl_f0_closed_plan")["hard_pass"])
        self.assertGreater(
            row("A0", "tl_f0_closed_plan")["metrics"]["environment_rejections"], 0
        )
        self.assertTrue(row("A1", "tl_f0_closed_plan")["hard_pass"])
        self.assertFalse(row("A0", "tl_f0_ambiguous_eth")["hard_pass"])
        self.assertFalse(row("A1", "tl_f0_ambiguous_eth")["hard_pass"])

    def test_state_aware_b_passes_without_the_list(self):
        b0 = self.report["cells"]["B0"]
        self.assertEqual(b0["summary"]["completions"], 3)
        self.assertEqual(b0["summary"]["environment_rejections"], 0)
        self.assertEqual(self.report["cells"]["B1"]["summary"]["completions"], 3)

    def test_interaction_tokens_and_deltas(self):
        inter = self.report["interaction"]
        self.assertGreater(inter["A_completion_delta"], 0)
        self.assertEqual(inter["B_completion_delta"], 0)
        self.assertLess(inter["A_environment_rejection_delta"], 0)
        self.assertGreater(inter["A_mean_input_token_delta"], 0)
        self.assertFalse(inter["ambiguous_hard_pass"]["A0"])
        self.assertFalse(inter["ambiguous_hard_pass"]["A1"])
        self.assertTrue(inter["ambiguous_hard_pass"]["B0"])


class H2SingleCaseToolsTests(unittest.TestCase):
    def test_run_one_case_state_only_has_same_tools_as_listed(self):
        a0 = run_one_case(
            task_id="tl_f0_closed_plan",
            model_id="A",
            policy=ObservationPolicy.STATE_ONLY,
            run_id="case_a0",
            budget=RunBudget(),
        )
        a1 = run_one_case(
            task_id="tl_f0_closed_plan",
            model_id="A",
            policy=ObservationPolicy.ALLOWED_ACTIONS,
            run_id="case_a1",
            budget=RunBudget(),
        )
        self.assertEqual(a0["prompt"]["tool_names"], a1["prompt"]["tool_names"])
        self.assertFalse(a0["prompt"]["has_allowed_actions"])
        self.assertTrue(a1["prompt"]["has_allowed_actions"])


if __name__ == "__main__":
    unittest.main()
