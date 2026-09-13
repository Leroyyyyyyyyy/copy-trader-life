from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.context.builder import ContextBuilder, group_history
from src.lab.context.compactor import InjectedSummarizer
from src.lab.domain import CompactionConfig, RunBudget, RunStatus
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view, load_for_eval
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.models.stub import StubModelAdapter
from src.lab.runtime.store import RunStore


class RecordingStub(StubModelAdapter):
    def __init__(self, script):
        super().__init__(script)
        self.requests: list[dict] = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


def _store() -> tuple[RunStore, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    return RunStore(Path(tmp.name) / "run.sqlite"), tmp


def _long_script(reads: int = 4) -> list[dict]:
    turns = [{"tool_calls": [{"name": "get_account_snapshot", "arguments": {}}]} for _ in range(reads)]
    turns.append(
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
        }
    )
    turns.append({"final": {"done": True}})
    return turns


def _run(policy: str, threshold: int = 1200, summarizer=None, reads: int = 4):
    store, tmp = _store()
    task, obs = load_agent_view("tl_f0_move_sl_plan_a")
    env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id=f"l2_{policy}")
    model = RecordingStub(_long_script(reads))
    builder = ContextBuilder(
        CompactionConfig(policy=policy, token_threshold=threshold, keep_recent_pairs=1),
        summarizer=summarizer,
    )
    result = AgentHarness(
        store=store,
        model=model,
        environment=env,
        budget=RunBudget(max_model_calls=16, max_tool_calls=16, max_mutates=1),
        context_builder=builder,
    ).run(task, run_id=f"l2_{policy}")
    return result, model, store, tmp, task


class GroupHistoryTests(unittest.TestCase):
    def test_does_not_split_action_observation_pair(self):
        events = [
            {
                "seq": 2,
                "kind": "model_turn",
                "payload": {"raw": {"tool_calls": [{"name": "get_plan"}, {"name": "get_account_snapshot"}]}},
            },
            {"seq": 3, "kind": "tool_result", "payload": {"name": "get_plan", "call_id": "a"}},
            {"seq": 4, "kind": "tool_result", "payload": {"name": "get_account_snapshot", "call_id": "b"}},
        ]
        groups = group_history(events)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["event_seqs"], [2, 3, 4])
        self.assertTrue(groups[0]["complete"])
        self.assertEqual(len(groups[0]["tool_results"]), 2)


class CompactionHarnessTests(unittest.TestCase):
    def test_long_run_triggers_compaction_and_keeps_evidence(self):
        compact, model, store, tmp, task = _run("compact", threshold=1200)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.assertEqual(compact.status, RunStatus.SUCCEEDED)
        self.assertTrue(compact.compaction_records)
        rec = compact.compaction_records[-1]
        self.assertLess(rec.tokens_after, rec.tokens_before)
        self.assertIn("plan_a", rec.summary)
        kinds = [e["kind"] for e in compact.events]
        self.assertIn("compaction", kinds)
        self.assertGreaterEqual(kinds.count("tool_result"), 5)
        last_req = model.requests[-1]
        self.assertEqual(last_req["history"]["policy"], "compact")
        self.assertLessEqual(len(last_req["history"]["turns"]), 1)
        wm = last_req["history"]["working_memory"]
        self.assertTrue(wm["source_event_ids"])
        self.assertIn("plan_a", wm["text"])
        _, _, spec = load_for_eval(task.task_id)
        score = verify_plan_decision(
            task_id=task.task_id,
            run_id=compact.run_id,
            eval_spec=spec,
            decision=compact.decision,
            final_snapshot=compact.final_snapshot,
        )
        self.assertTrue(score.hard_pass)

    def test_full_vs_compact_total_input_tokens(self):
        compact, _, store_c, tmp_c, _ = _run("compact", threshold=1200)
        self.addCleanup(tmp_c.cleanup)
        self.addCleanup(store_c.close)
        full, _, store_f, tmp_f, _ = _run("full", threshold=1200)
        self.addCleanup(tmp_f.cleanup)
        self.addCleanup(store_f.close)
        last_compact = [
            e["payload"]["active_tokens"]
            for e in compact.events
            if e["kind"] == "model_turn"
        ][-1]
        last_full = [
            e["payload"]["active_tokens"]
            for e in full.events
            if e["kind"] == "model_turn"
        ][-1]
        self.assertLess(last_compact, last_full)
        # Whole-run input includes summary cost; last-round shrink is not enough to claim savings.
        self.assertGreater(compact.usage.summary_tokens, 0)
        self.assertGreater(full.usage.input_tokens, 0)

    def test_wrong_summary_price_does_not_rewrite_trace(self):
        bad = InjectedSummarizer(text="plan_a stop_loss is 9999 (false summary)")
        result, model, store, tmp, _ = _run("compact", threshold=1200, summarizer=bad)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.assertIn("9999", result.compaction_records[-1].summary)
        tool_payloads = [e["payload"] for e in result.events if e["kind"] == "tool_result"]
        blob = str(tool_payloads)
        self.assertNotIn("9999", blob)
        self.assertIn("1800", blob)
        last_turns = model.requests[-1]["history"]["turns"]
        self.assertTrue(last_turns)
        self.assertIn("tool_results", last_turns[0])

    def test_summary_failure_uses_declared_fallback(self):
        result, model, store, tmp, _ = _run(
            "compact", threshold=1200, summarizer=InjectedSummarizer(fail=True)
        )
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        rec = result.compaction_records[-1]
        self.assertTrue(rec.fallback)
        self.assertIn("summary_failed", rec.summary)
        wm = model.requests[-1]["history"]["working_memory"]
        self.assertIn("source_event_ids", wm)

    def test_uncompactable_prefix_fails_loudly(self):
        result, _, store, tmp, _ = _run("compact", threshold=1, reads=3)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertIn("context_budget_error", result.terminal_reason)


if __name__ == "__main__":
    unittest.main()
