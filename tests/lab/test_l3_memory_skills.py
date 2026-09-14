from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.lab.domain import MemoryRecord, MemoryType, RunStatus
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.evals.loader import load_agent_view
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.memory.store import LabelInMemoryError, MemoryStore
from src.lab.models.stub import StubModelAdapter
from src.lab.runtime.store import RunStore
from src.lab.skills.registry import SkillError, SkillRegistry

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "experiments" / "skill_library"


class RecordingStub(StubModelAdapter):
    def __init__(self, script):
        super().__init__(script)
        self.requests: list[dict] = []

    def generate(self, request):
        self.requests.append(request)
        return super().generate(request)


def _tmp_store() -> tuple[RunStore, tempfile.TemporaryDirectory[str]]:
    tmp = tempfile.TemporaryDirectory()
    return RunStore(Path(tmp.name) / "run.sqlite"), tmp


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mem = MemoryStore(Path(self.tmp.name) / "mem.sqlite", snapshot_id="snap_a")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.mem.close)

    def test_expired_and_superseded_are_hidden(self):
        old = self.mem.publish(
            MemoryRecord(
                memory_id="m1",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="ETH cost alias is stale 1700",
                valid_to="2026-08-01T00:00:00Z",
            )
        )
        live = self.mem.publish(
            MemoryRecord(
                memory_id="m2",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="ETH long from message A is plan_a style",
            )
        )
        newer = self.mem.supersede(
            live.memory_id,
            MemoryRecord(
                memory_id="m3",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="ETH follow-up updates the filled ETH plan only",
                version=2,
            ),
        )
        hits = self.mem.retrieve(query="ETH", visible_at="2026-09-01T12:00:00Z")
        ids = {h.memory_id for h in hits}
        self.assertNotIn(old.memory_id, ids)
        self.assertNotIn(live.memory_id, ids)
        self.assertIn(newer.memory_id, ids)
        self.assertEqual(newer.supersedes, live.memory_id)

    def test_conflicts_returned_together(self):
        self.mem.publish(
            MemoryRecord(
                memory_id="c1",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="source chart says SL near 1650",
                source_event_id="chart_eth_a",
            )
        )
        self.mem.publish(
            MemoryRecord(
                memory_id="c2",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="source text says move SL to cost 1800",
                source_event_id="message:C",
            )
        )
        hits = self.mem.retrieve(scope="ETH")
        self.assertEqual(len(hits), 2)
        self.assertEqual({h.source_event_id for h in hits}, {"chart_eth_a", "message:C"})

    def test_overlay_does_not_copy_to_new_snapshot(self):
        self.mem.publish(
            MemoryRecord(
                memory_id="pub",
                memory_type=MemoryType.FACT,
                scope="*",
                content="published channel style",
            )
        )
        self.mem.propose(
            MemoryRecord(
                memory_id="ov",
                memory_type=MemoryType.FACT,
                scope="*",
                content="run overlay only",
            )
        )
        dest = self.mem.copy_snapshot(Path(self.tmp.name) / "mem_b.sqlite", "snap_b")
        self.addCleanup(dest.close)
        dest_hits = dest.retrieve(include_overlay=True)
        self.assertEqual([h.content for h in dest_hits], ["published channel style"])
        self.assertTrue(any("overlay" in h.content for h in self.mem.retrieve()))

    def test_label_text_rejected(self):
        with self.assertRaises(LabelInMemoryError):
            self.mem.propose(
                MemoryRecord(
                    memory_id="bad",
                    memory_type=MemoryType.EVAL,
                    scope="*",
                    content="acceptable_outcomes say plan_a sl 1800",
                )
            )


class SkillRegistryTests(unittest.TestCase):
    def test_catalog_has_no_body(self):
        reg = SkillRegistry(SKILL_ROOT)
        reg.load_directory()
        catalog = reg.catalog()
        self.assertEqual({e["name"] for e in catalog}, {"link_followup", "verify_exit_protect"})
        for entry in catalog:
            self.assertNotIn("body", entry)
            self.assertIn("description", entry)

    def test_invoke_returns_body_and_unknown_is_recoverable(self):
        reg = SkillRegistry(SKILL_ROOT)
        reg.load_directory()
        body = reg.invoke("link_followup")
        self.assertIn("列出当前可见消息", body["body"])
        self.assertNotIn("plan_a", body["body"])
        miss = reg.invoke("not_a_skill")
        self.assertEqual(miss["error_code"], "unknown_skill")
        self.assertTrue(miss["retryable"])

    def test_duplicate_and_outside_root_rejected(self):
        reg = SkillRegistry(SKILL_ROOT)
        reg.load_directory()
        with self.assertRaises(SkillError):
            reg.register_file(SKILL_ROOT / "link_followup" / "SKILL.md")
        with self.assertRaises(SkillError):
            reg.register_file(Path("/tmp/SKILL.md"))

    def test_missing_frontmatter_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bad = root / "x" / "SKILL.md"
        bad.parent.mkdir()
        bad.write_text("no frontmatter\n", encoding="utf-8")
        reg = SkillRegistry(root)
        with self.assertRaises(SkillError):
            reg.load_directory()


class SkillHarnessTests(unittest.TestCase):
    def _run(self, mode: str, script: list[dict], run_id: str):
        store, tmp = _tmp_store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id=run_id)
        skills = SkillRegistry(SKILL_ROOT)
        skills.load_directory()
        model = RecordingStub(script)
        result = AgentHarness(
            store=store,
            model=model,
            environment=env,
            skills=skills,
            skill_mode=mode,
        ).run(task, run_id=run_id)
        return result, model

    def test_catalog_mode_loads_on_demand(self):
        result, model = self._run(
            "catalog",
            [
                {"tool_calls": [{"name": "invoke_skill", "arguments": {"name": "verify_exit_protect"}}]},
                {
                    "tool_calls": [
                        {
                            "name": "submit_plan_update",
                            "arguments": {
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C", "skill:verify_exit_protect"],
                                "expected_state_version": 3,
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ],
            "run_skill_od",
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        first = model.requests[0]
        self.assertTrue(first["skill_catalog"])
        self.assertEqual(first["loaded_skills"], [])
        self.assertNotIn("核验退出保护更新", str(first["skill_catalog"]))
        second = model.requests[1]
        self.assertTrue(second["loaded_skills"])
        self.assertIn("核验退出保护更新", second["loaded_skills"][0]["body"])
        tool_names = {t["name"] for t in first["tools"]}
        self.assertIn("invoke_skill", tool_names)
        self.assertIn("submit_plan_update", tool_names)
        after_invoke = [e for e in result.events if e["kind"] == "tool_result"]
        self.assertEqual(after_invoke[0]["payload"]["name"], "invoke_skill")
        self.assertEqual(
            {t["name"] for t in second["tools"]},
            tool_names,
        )

    def test_none_mode_hides_skill_bodies_and_tool(self):
        result, model = self._run(
            "none",
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
                {"final": {"done": True}},
            ],
            "run_skill_none",
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        first = model.requests[0]
        self.assertEqual(first["skill_catalog"], [])
        self.assertEqual(first["loaded_skills"], [])
        self.assertNotIn("invoke_skill", {t["name"] for t in first["tools"]})

    def test_preload_includes_bodies_up_front(self):
        _, model = self._run(
            "preload",
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
                {"final": {"done": True}},
            ],
            "run_skill_pre",
        )
        first = model.requests[0]
        bodies = " ".join(s["body"] for s in first["loaded_skills"])
        self.assertIn("关联跟帖", bodies)
        self.assertIn("核验退出保护更新", bodies)

    def test_new_run_does_not_inherit_loaded_bodies(self):
        store, tmp = _tmp_store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        skills = SkillRegistry(SKILL_ROOT)
        skills.load_directory()
        env1 = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="r1")
        model1 = RecordingStub(
            [
                {"tool_calls": [{"name": "invoke_skill", "arguments": {"name": "link_followup"}}]},
                {"final": {"done": True}},
            ]
        )
        AgentHarness(
            store=store, model=model1, environment=env1, skills=skills, skill_mode="catalog"
        ).run(task, run_id="r1")
        env2 = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="r2")
        model2 = RecordingStub([{"final": {"done": True}}])
        AgentHarness(
            store=store, model=model2, environment=env2, skills=skills, skill_mode="catalog"
        ).run(task, run_id="r2")
        self.assertTrue(model1.requests[1]["loaded_skills"])
        self.assertEqual(model2.requests[0]["loaded_skills"], [])


class MemoryHarnessTests(unittest.TestCase):
    def test_published_memory_appears_in_prompt(self):
        store, tmp = _tmp_store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        mem = MemoryStore(Path(tmp.name) / "mem.sqlite")
        self.addCleanup(mem.close)
        mem.publish(
            MemoryRecord(
                memory_id="hint",
                memory_type=MemoryType.FACT,
                scope="ETH",
                content="filled ETH plan opened from message A should take the stop-to-cost update",
            )
        )
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        env = PlanSimulationEnvironment(obs, SimulationGateway(store), run_id="run_mem")
        model = RecordingStub(
            [
                {
                    "tool_calls": [
                        {
                            "name": "submit_plan_update",
                            "arguments": {
                                "plan_id": "plan_a",
                                "stop_loss": 1800,
                                "evidence_refs": ["message:C", "memory:hint"],
                                "expected_state_version": 3,
                            },
                        }
                    ]
                },
                {"final": {"done": True}},
            ]
        )
        result = AgentHarness(store=store, model=model, environment=env, memory=mem).run(
            task, run_id="run_mem"
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        contents = [m["content"] for m in model.requests[0]["memories"]]
        self.assertTrue(any("message A" in c for c in contents))


if __name__ == "__main__":
    unittest.main()
