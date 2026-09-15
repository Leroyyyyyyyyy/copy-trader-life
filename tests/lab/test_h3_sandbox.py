from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.domain import RunBudget, RunStatus, ToolStatus
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.models.stub import StubModelAdapter
from src.lab.runtime.store import RunStore
from src.lab.sandbox.disabled import DisabledSandbox
from src.lab.sandbox.docker import DockerSandbox, docker_available
from src.lab.sandbox.snapshot_sim import simulate_action

MULTI_SIM_CODE = """
snap = get_snapshot()
eth = [p for p in snap["plans"] if p["symbol"] == "ETH"]
btc = [p for p in snap["plans"] if p["symbol"] == "BTC"]
r_eth = simulate_action({
    "kind": "update",
    "plan_id": eth[0]["plan_id"],
    "stop_loss": eth[0]["cost_basis"],
    "evidence_refs": ["message:C", "plan:" + eth[0]["plan_id"]],
})
r_btc = simulate_action({
    "kind": "update",
    "plan_id": btc[0]["plan_id"],
    "stop_loss": 1,
    "evidence_refs": ["message:C", "plan:" + btc[0]["plan_id"]],
})
if r_eth["legal"]:
    propose_action({
        "kind": "update",
        "plan_id": eth[0]["plan_id"],
        "stop_loss": eth[0]["cost_basis"],
        "evidence_refs": ["message:C", "plan:" + eth[0]["plan_id"]],
        "expected_state_version": snap["state_version"],
    })
"""

BOOM_CODE = "raise ValueError('guest boom')\n"


def _store() -> tuple[RunStore, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    return RunStore(Path(tmp.name) / "run.sqlite"), tmp


class SnapshotSimTests(unittest.TestCase):
    def test_fork_does_not_mutate_input_and_closed_is_illegal(self):
        snap = {
            "state_version": 5,
            "submit_count": 0,
            "plans": [
                {
                    "plan_id": "plan_a",
                    "symbol": "ETH",
                    "status": "closed",
                    "stop_loss": 1650,
                    "cost_basis": 1800,
                }
            ],
        }
        original = snap["plans"][0]["stop_loss"]
        out = simulate_action(
            snap,
            {
                "kind": "update",
                "plan_id": "plan_a",
                "stop_loss": 1800,
                "evidence_refs": ["message:C"],
            },
        )
        self.assertFalse(out["legal"])
        self.assertIn("plan_not_filled:closed", out["constraint_results"])
        self.assertEqual(snap["plans"][0]["stop_loss"], original)
        self.assertEqual(snap["state_version"], 5)
        self.assertFalse(out["shared_mutated"])


class DisabledSandboxTests(unittest.TestCase):
    def test_run_program_not_registered_and_never_host_execs(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(
            obs, SimulationGateway(store), run_id="off", sandbox=DisabledSandbox()
        )
        names = env.tool_registry().names()
        self.assertNotIn("run_program", names)
        self.assertIn("simulate_action", names)
        src = Path("src/lab/sandbox/docker.py").read_text(encoding="utf-8")
        self.assertNotIn("exec(", src)
        self.assertNotIn("eval(", src)


class SimulateDoesNotCommitTests(unittest.TestCase):
    def test_host_simulate_leaves_shared_account(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        _, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="sim")
        before = env.gateway.snapshot()
        out = env.handle_tool(
            "simulate_action",
            {
                "kind": "update",
                "plan_id": "plan_a",
                "stop_loss": 1800,
                "evidence_refs": ["message:C"],
            },
        )
        self.assertTrue(out["legal"])
        self.assertEqual(out["simulated_state"]["plans"][0]["stop_loss"], 1800)
        after = env.gateway.snapshot()
        self.assertEqual(after["submit_count"], 0)
        self.assertEqual(after["state_version"], before["state_version"])
        plan_a = next(p for p in after["plans"] if p["plan_id"] == "plan_a")
        self.assertEqual(plan_a["stop_loss"], 1650)


class VersionConflictTests(unittest.TestCase):
    def test_stale_commit_rejected_after_real_submit(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="stale")
        model = StubModelAdapter(
            [
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
                {
                    "tool_calls": [
                        {
                            "name": "commit_simulated_action",
                            "arguments": {
                                "kind": "update",
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C"],
                                "expected_state_version": 3,
                                "idempotency_key": "second",
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        result = AgentHarness(store=store, model=model, environment=env).run(
            task, run_id="stale"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.final_snapshot.submit_count, 1)
        errors = [
            e["payload"].get("error_code")
            for e in result.events
            if e["kind"] == "tool_result"
        ]
        self.assertTrue(any(e and "stale_state_version" in e for e in errors))


@unittest.skipUnless(docker_available(), "Docker daemon is required for H3 run_program")
class DockerProgramTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = DockerSandbox(timeout_sec=30)

    def _env(self, run_id: str):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(
            obs, SimulationGateway(store), run_id=run_id, sandbox=self.sandbox
        )
        return store, task, env

    def test_multi_sim_one_commit(self):
        store, task, env = self._env("h3_multi")
        before = env.gateway.snapshot()
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "run_program", "arguments": {"code": MULTI_SIM_CODE}}]},
                {
                    "tool_calls": [
                        {
                            "name": "commit_simulated_action",
                            "arguments": {
                                "kind": "update",
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C", "plan:plan_a"],
                                "expected_state_version": before["state_version"],
                                "idempotency_key": "pick_eth",
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        result = AgentHarness(
            store=store,
            model=model,
            environment=env,
            budget=RunBudget(max_model_calls=8, max_tool_calls=16, max_mutates=1),
        ).run(task, run_id="h3_multi")
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(result.final_snapshot.submit_count, 1)
        plan_a = next(p for p in result.final_snapshot.plans if p.plan_id == "plan_a")
        plan_btc = next(p for p in result.final_snapshot.plans if p.plan_id == "plan_btc")
        self.assertEqual(plan_a.stop_loss, 1800)
        self.assertEqual(plan_btc.stop_loss, 100000)
        self.assertGreaterEqual(result.usage.program_tool_calls, 2)
        self.assertGreaterEqual(result.usage.tool_calls, 3)
        run_events = [e for e in result.events if e["kind"] == "tool_result"]
        obs = run_events[0]["payload"]
        self.assertEqual(obs.get("expanded_calls"), 2)
        inner = obs.get("payload") or {}
        self.assertIsNone(inner.get("program_error"))
        self.assertNotIn("commit", {t["name"] for t in inner.get("tool_trace") or []})
        _, _, spec = load_for_eval(task.task_id)
        score = verify_plan_decision(
            task_id=task.task_id,
            run_id=result.run_id,
            eval_spec=spec,
            decision=result.decision,
            final_snapshot=result.final_snapshot,
        )
        self.assertTrue(score.hard_pass)
        leftover = __import__("subprocess").run(
            ["docker", "ps", "-aq", "--filter", "name=tl-h3-"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertFalse((leftover.stdout or "").strip())

    def test_program_error_then_recover(self):
        store, task, env = self._env("h3_boom")
        model = StubModelAdapter(
            [
                {"tool_calls": [{"name": "run_program", "arguments": {"code": BOOM_CODE}}]},
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
            task, run_id="h3_boom"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        first = next(e for e in result.events if e["kind"] == "tool_result")
        self.assertEqual(first["payload"]["error_code"], "program_error")
        self.assertEqual(first["payload"]["status"], ToolStatus.ERROR.value)
        self.assertEqual(result.final_snapshot.submit_count, 1)
        self.assertTrue(first["payload"].get("retryable"))

    def test_same_input_is_deterministic(self):
        store, task, env = self._env("h3_det")
        a = env.sandbox.run_program(MULTI_SIM_CODE, env.gateway.snapshot())
        b = env.sandbox.run_program(MULTI_SIM_CODE, env.gateway.snapshot())
        self.assertFalse(a.infrastructure)
        self.assertEqual(a.expanded_calls, b.expanded_calls)
        self.assertEqual(a.proposed_action, b.proposed_action)
        self.assertEqual(env.gateway.submit_count, 0)


if __name__ == "__main__":
    unittest.main()
