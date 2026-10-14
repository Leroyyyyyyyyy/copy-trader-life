from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.lab.domain import DecisionKind, RunStatus, TraceSummary
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.runtime.store import RunStore
from src.lab.workflows import InjectedCrash, make_workflow_adapter

try:
    import langgraph  # noqa: F401

    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False


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


def _run(backend: str, task_id: str, run_id: str, **kwargs):
    store, tmp, task, env = _env(task_id, run_id)
    adapter = make_workflow_adapter(backend, environment=env, store=store, **kwargs)
    result = adapter.run_or_resume(task, run_id=run_id)
    return store, tmp, task, env, adapter, result


class NativeLangGraphParityTests(unittest.TestCase):
    def test_same_tasks_same_verifier(self):
        if not HAS_LANGGRAPH:
            self.skipTest("langgraph is not installed")
        for task_id in (
            "tl_f0_move_sl_plan_a",
            "tl_f0_closed_plan",
            "tl_f0_ambiguous_eth",
        ):
            native = _run("native", task_id, f"nat_{task_id}")
            graph = _run("langgraph", task_id, f"lg_{task_id}")
            self.addCleanup(native[1].cleanup)
            self.addCleanup(graph[1].cleanup)
            self.addCleanup(native[0].close)
            self.addCleanup(graph[0].close)
            n_task, n_result = native[2], native[5]
            g_task, g_result = graph[2], graph[5]
            self.assertEqual(n_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(g_result.status, RunStatus.SUCCEEDED)
            self.assertEqual(n_result.decision.kind, g_result.decision.kind)
            self.assertEqual(
                n_result.decision.referenced_plan_id,
                g_result.decision.referenced_plan_id,
            )
            self.assertEqual(
                n_result.decision.proposed_changes,
                g_result.decision.proposed_changes,
            )
            self.assertEqual(n_result.final_snapshot.submit_count, 1)
            self.assertEqual(g_result.final_snapshot.submit_count, 1)
            self.assertTrue(_score(n_task, n_result).hard_pass)
            self.assertTrue(_score(g_task, g_result).hard_pass)

    def test_backends_do_not_share_an_account(self):
        if not HAS_LANGGRAPH:
            self.skipTest("langgraph is not installed")
        native = _run("native", "tl_f0_move_sl_plan_a", "iso_native")
        graph = _run("langgraph", "tl_f0_move_sl_plan_a", "iso_graph")
        self.addCleanup(native[1].cleanup)
        self.addCleanup(graph[1].cleanup)
        self.addCleanup(native[0].close)
        self.addCleanup(graph[0].close)
        n_env, g_env = native[3], graph[3]
        self.assertIsNot(n_env.gateway, g_env.gateway)
        self.assertEqual(n_env.gateway.submit_count, 1)
        self.assertEqual(g_env.gateway.submit_count, 1)
        n_plans = {p.plan_id: p for p in native[5].final_snapshot.plans}
        g_plans = {p.plan_id: p for p in graph[5].final_snapshot.plans}
        self.assertEqual(n_plans["plan_a"].stop_loss, 1800)
        self.assertEqual(g_plans["plan_a"].stop_loss, 1800)


class CheckpointAndInterruptTests(unittest.TestCase):
    def test_native_checkpoint_omits_account_snapshot(self):
        store, tmp, task, env, _, result = _run(
            "native", "tl_f0_move_sl_plan_a", "ckpt_shape"
        )
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        payload = store.get_checkpoint("ckpt_shape")
        self.assertEqual(payload["next_node"], "finish")
        self.assertIn("classified", payload["state"])
        self.assertIn("next_node", payload)
        self.assertNotIn("submit_count", json.dumps(payload))
        self.assertNotIn("plans", payload["state"])
        self.assertNotIn("snapshot", payload["state"])

    def test_native_interrupt_then_new_adapter_resumes(self):
        store, tmp, task, env = _env("tl_f0_move_sl_plan_a", "int_native")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        paused = make_workflow_adapter(
            "native",
            environment=env,
            store=store,
            interrupt_before=("commit",),
        ).run_or_resume(task, run_id="int_native")
        self.assertEqual(paused.status, RunStatus.INTERRUPTED)
        self.assertEqual(env.gateway.submit_count, 0)
        self.assertEqual(store.get_checkpoint("int_native")["next_node"], "commit")
        resumed = make_workflow_adapter(
            "native", environment=env, store=store
        ).run_or_resume(task, run_id="int_native")
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(env.gateway.submit_count, 1)
        self.assertTrue(_score(task, resumed).hard_pass)

    @unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed")
    def test_langgraph_interrupt_before_commit(self):
        store, tmp, task, env = _env("tl_f0_move_sl_plan_a", "int_graph")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        adapter = make_workflow_adapter(
            "langgraph",
            environment=env,
            store=store,
            interrupt_before=("commit",),
        )
        paused = adapter.run_or_resume(task, run_id="int_graph")
        self.assertEqual(paused.status, RunStatus.INTERRUPTED)
        self.assertEqual(env.gateway.submit_count, 0)
        resumed = adapter.run_or_resume(task, run_id="int_graph")
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(env.gateway.submit_count, 1)
        self.assertTrue(_score(task, resumed).hard_pass)

    def test_concurrent_fan_out_still_one_commit(self):
        store, tmp, task, env, _, result = _run(
            "native",
            "tl_f0_move_sl_plan_a",
            "fan_native",
            mode="concurrent",
            chart_delay_sec=0.05,
            market_delay_sec=0.05,
        )
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.assertEqual(result.final_snapshot.submit_count, 1)
        self.assertTrue(_score(task, result).hard_pass)


class CrashRecoveryTests(unittest.TestCase):
    def test_native_crash_after_commit_reuses_receipt(self):
        store, tmp, task, env = _env("tl_f0_move_sl_plan_a", "crash_after_n")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        adapter = make_workflow_adapter("native", environment=env, store=store)
        with self.assertRaises(InjectedCrash) as caught:
            adapter.run_or_resume(
                task, run_id="crash_after_n", crash_after_commit=True
            )
        self.assertIn("after_commit_before_checkpoint", str(caught.exception))
        self.assertEqual(env.gateway.submit_count, 1)
        ckpt = store.get_checkpoint("crash_after_n")
        self.assertEqual(ckpt["next_node"], "commit")
        receipt = store.get_commit("crash_after_n:decision:1")
        self.assertIsNotNone(receipt)
        resumed = make_workflow_adapter(
            "native", environment=env, store=store
        ).run_or_resume(task, run_id="crash_after_n")
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(env.gateway.submit_count, 1)
        plans = {p.plan_id: p for p in resumed.final_snapshot.plans}
        self.assertEqual(plans["plan_a"].stop_loss, 1800)
        self.assertEqual(plans["plan_btc"].stop_loss, 100000)
        self.assertTrue(_score(task, resumed).hard_pass)

    @unittest.skipUnless(HAS_LANGGRAPH, "langgraph is not installed")
    def test_langgraph_crash_after_commit_reuses_receipt(self):
        store, tmp, task, env = _env("tl_f0_move_sl_plan_a", "crash_after_g")
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        adapter = make_workflow_adapter("langgraph", environment=env, store=store)
        with self.assertRaises(InjectedCrash):
            adapter.run_or_resume(
                task, run_id="crash_after_g", crash_after_commit=True
            )
        self.assertEqual(env.gateway.submit_count, 1)
        resumed = adapter.run_or_resume(task, run_id="crash_after_g")
        self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
        self.assertEqual(env.gateway.submit_count, 1)
        plans = {p.plan_id: p for p in resumed.final_snapshot.plans}
        self.assertEqual(plans["plan_a"].stop_loss, 1800)
        self.assertTrue(_score(task, resumed).hard_pass)

    def test_crash_before_commit_then_resume_commits_once(self):
        backends = ["native"]
        if HAS_LANGGRAPH:
            backends.append("langgraph")
        for backend in backends:
            with self.subTest(backend=backend):
                run_id = f"crash_before_{backend}"
                store, tmp, task, env = _env("tl_f0_move_sl_plan_a", run_id)
                self.addCleanup(tmp.cleanup)
                self.addCleanup(store.close)
                adapter = make_workflow_adapter(backend, environment=env, store=store)
                with self.assertRaises(InjectedCrash) as caught:
                    adapter.run_or_resume(
                        task, run_id=run_id, crash_before_commit=True
                    )
                self.assertIn("before_commit", str(caught.exception))
                self.assertEqual(env.gateway.submit_count, 0)
                if backend == "native":
                    resumed = make_workflow_adapter(
                        "native", environment=env, store=store
                    ).run_or_resume(task, run_id=run_id)
                else:
                    resumed = adapter.run_or_resume(task, run_id=run_id)
                self.assertEqual(resumed.status, RunStatus.SUCCEEDED)
                self.assertEqual(env.gateway.submit_count, 1)
                self.assertTrue(_score(task, resumed).hard_pass)


if __name__ == "__main__":
    unittest.main()
