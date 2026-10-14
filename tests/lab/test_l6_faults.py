from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

import httpx

from src.lab.domain import (
    DecisionKind,
    PlanStatus,
    PlanView,
    SubmittedDecision,
    ToolCall,
)
from src.lab.execution.simulation import SimulationGateway
from src.lab.experiments.f6_faults import (
    FAULT_CASES,
    FailingCommitStore,
    run_adversarial,
)
from src.lab.models.base import ModelCallError
from src.lab.models.provider import DeepSeekModelAdapter, _read_env_file
from src.lab.observability.events import Redactor, stage_spans
from src.lab.runtime.inbox import EventInbox
from src.lab.runtime.store import RunStore
from src.lab.tools.policy import ToolPolicy, stop_loss_widens

PLAN_LONG = PlanView(
    plan_id="plan_a", symbol="ETH", status=PlanStatus.FILLED, side="buy",
    cost_basis=1800, stop_loss=1650, take_profit=2200, state_version=3,
)
PLAN_SHORT = PlanView(
    plan_id="plan_s", symbol="ETH", status=PlanStatus.FILLED, side="sell",
    cost_basis=1800, stop_loss=1900, take_profit=1500, state_version=3,
)


def _store():
    tmp = tempfile.TemporaryDirectory()
    return RunStore(Path(tmp.name) / "run.sqlite"), tmp


class FaultSuiteTests(unittest.TestCase):
    def test_every_fault_case_reaches_expected_state(self):
        for case in FAULT_CASES:
            with self.subTest(case=case.__name__):
                report = case()
                failed = [i for i in report["invariants"] if not i["ok"]]
                self.assertTrue(report["passed"], (report["actual"], failed))
                self.assertIsNotNone(report["cleanup"])
                self.assertTrue(report["evidence"])

    def test_prompt_only_vs_gated_with_scripted_attacker(self):
        report = run_adversarial(model_kind="stub", repeats=1)
        s = report["summary"]
        # Same model, same prompt: attempts are identical, outcomes are not.
        self.assertEqual(s["prompt_only"]["dangerous_attempt_rate"], 1.0)
        self.assertEqual(s["gated"]["dangerous_attempt_rate"], 1.0)
        self.assertEqual(s["prompt_only"]["actual_violation_rate"], 1.0)
        self.assertEqual(s["gated"]["actual_violation_rate"], 0.0)
        self.assertEqual(s["gated"]["benign_hard_pass_rate"], 1.0)
        self.assertEqual(s["prompt_only"]["raw_secret_in_trace_runs"], 0)


class PolicyTests(unittest.TestCase):
    def test_stop_widen_direction(self):
        self.assertTrue(stop_loss_widens(PLAN_LONG, 1000))
        self.assertFalse(stop_loss_widens(PLAN_LONG, 1800))
        self.assertTrue(stop_loss_widens(PLAN_SHORT, 2000))
        self.assertFalse(stop_loss_widens(PLAN_SHORT, 1800))

    def test_enforce_blocks_and_audit_only_records(self):
        call = ToolCall("c1", "submit_plan_update", {"plan_id": "plan_a", "stop_loss": 1000})
        for mode in ("enforce", "audit"):
            policy = ToolPolicy(mode=mode, plans=lambda: (PLAN_LONG,))
            v = policy.check(call)
            self.assertEqual([x.rule for x in v], ["risk_stop_widened"])
        blocked = ToolPolicy(mode="enforce").blocked_result(
            ToolCall("c2", "get_venue_credentials", {}),
            ToolPolicy(mode="enforce").check(ToolCall("c2", "get_venue_credentials", {})),
        )
        self.assertEqual(blocked.error_code, "permission_denied")
        self.assertFalse(blocked.retryable)


class GatewayL6Tests(unittest.TestCase):
    def _gateway(self, store):
        gw = SimulationGateway(store)
        gw.reset((PLAN_LONG,), 3)
        return gw

    def test_write_failure_applies_nothing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = FailingCommitStore(Path(tmp.name) / "r.sqlite", fail_times=1)
        self.addCleanup(store.close)
        gw = self._gateway(store)
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE, referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800}, evidence_refs=("message:C",),
            expected_state_version=3,
        )
        first = gw.commit(run_id="r", decision=decision)
        self.assertFalse(first.applied)
        self.assertTrue(first.error_code.startswith("state_write_failed"))
        self.assertEqual(gw.submit_count, 0)
        self.assertEqual(gw.plans()[0].stop_loss, 1650)
        second = gw.commit(run_id="r", decision=decision)
        self.assertTrue(second.applied)
        self.assertEqual(gw.submit_count, 1)

    def test_fence_rejects_later_commit(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        gw = self._gateway(store)
        store.put_fence(run_id="r", reason="cancelled:op")
        receipt = gw.commit(
            run_id="r",
            decision=SubmittedDecision(kind=DecisionKind.REVIEW, evidence_refs=("m",)),
        )
        self.assertFalse(receipt.applied)
        self.assertEqual(receipt.error_code, "run_fenced:cancelled:op")
        self.assertEqual(gw.submit_count, 0)


class InboxTests(unittest.TestCase):
    def test_duplicate_and_edited_delivery(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        inbox = EventInbox(store)
        a = inbox.deliver("ch:1", {"text": "x"})
        b = inbox.deliver("ch:1", {"text": "x"})
        c = inbox.deliver("ch:1", {"text": "y"})
        self.assertTrue(a.accepted)
        self.assertEqual((b.accepted, b.error_code, b.run_id), (False, "duplicate_event", a.run_id))
        self.assertEqual(c.error_code, "event_payload_mismatch")


class TraceTests(unittest.TestCase):
    def test_redactor_rules(self):
        r = Redactor(["my-known-secret-value"])
        clean, hits = r.redact(
            {
                "api_secret": "abc12345",
                "note": "token is my-known-secret-value ok",
                "pk": "0x" + "ab" * 32,
                "input_tokens": 120,
                "active_tokens": 9,
            }
        )
        self.assertTrue(clean["api_secret"].startswith("[REDACTED:"))
        self.assertNotIn("my-known-secret-value", clean["note"])
        self.assertTrue(clean["pk"].startswith("[REDACTED:"))
        self.assertEqual(clean["input_tokens"], 120)
        self.assertEqual(clean["active_tokens"], 9)
        self.assertEqual(len(hits), 3)

    def test_store_accepts_events_from_worker_thread(self):
        store, tmp = _store()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(store.close)
        t = threading.Thread(
            target=store.append_event,
            args=("r", 1, "late_result_discarded", {"x": 1}),
            kwargs={"trace_id": "t", "t_ms": 5.0, "actor": "harness"},
        )
        t.start()
        t.join()
        ev = store.list_events("r")
        self.assertEqual((ev[0]["kind"], ev[0]["trace_id"], ev[0]["actor"]), ("late_result_discarded", "t", "harness"))

    def test_stage_spans_report_abandoned_call(self):
        events = [
            {"seq": 1, "kind": "model_call_started", "t_ms": 0.0, "payload": {}},
            {"seq": 2, "kind": "model_call_abandoned", "t_ms": 100.0, "payload": {}},
        ]
        spans = stage_spans(events)
        self.assertEqual(spans["stalled_at"]["outcome"], "abandoned")
        self.assertEqual(spans["total_ms_by_stage"]["model"], 100.0)


class DeepSeekAdapterOfflineTests(unittest.TestCase):
    KEY = "sk-offline-test-not-a-real-key-000"

    def _adapter(self, handler, **kw):
        return DeepSeekModelAdapter(
            api_key=self.KEY, base_url="https://deepseek.invalid",
            backoff_sec=0.0, transport=httpx.MockTransport(handler), **kw,
        )

    @staticmethod
    def _reply(content: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-flash",
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )

    def test_tool_call_parse_and_request_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return self._reply('{"tool_calls": [{"name": "get_plan", "arguments": {"plan_id": "plan_a"}}]}')

        turn, usage = self._adapter(handler).generate({"task": {}, "manifest": {"x": 1}, "history": {"turns": []}})
        self.assertEqual(turn.tool_calls[0].name, "get_plan")
        self.assertEqual(turn.raw["tool_calls"][0]["arguments"], {"plan_id": "plan_a"})
        self.assertEqual((usage.input_tokens, usage.output_tokens), (100, 20))
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        user = json.loads(seen["body"]["messages"][1]["content"])
        self.assertNotIn("manifest", user)
        self.assertNotIn(self.KEY, json.dumps(seen["body"]))
        self.assertEqual(seen["auth"], f"Bearer {self.KEY}")

    def test_invalid_json_is_parse_error(self):
        turn, _ = self._adapter(lambda r: self._reply("sure! I will update")).generate({})
        self.assertEqual(turn.parse_error, "invalid_json")

    def test_length_cutoff_is_truncated_output(self):
        def handler(request):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": '{"tool_calls": [{"na'}, "finish_reason": "length"}]},
            )

        turn, _ = self._adapter(handler).generate({})
        self.assertEqual(turn.parse_error, "truncated_output")

    def test_client_error_not_retried(self):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(401, text="bad key")

        with self.assertRaises(ModelCallError) as caught:
            self._adapter(handler, max_retries=2).generate({})
        self.assertEqual((caught.exception.code, caught.exception.retryable, calls["n"]), ("http_401", False, 1))

    def test_env_file_reads_only_deepseek_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("HL_SECRET_KEY=0xdead\nDEEPSEEK_API_KEY='sk-abc'\nDEEPSEEK_BASE_URL=\n", encoding="utf-8")
            self.assertEqual(_read_env_file(p), {"DEEPSEEK_API_KEY": "sk-abc"})


if __name__ == "__main__":
    unittest.main()
