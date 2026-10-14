from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.lab import cli
from src.lab.domain import DecisionKind, SubmittedDecision
from src.lab.execution.simulation import SimulationGateway
from src.lab.experiments.f7_review import RuleAgentStub, agent_initial_score, run_eval
from src.lab.interaction.coordinator import run_task
from src.lab.interaction.review import ReviewService
from src.lab.runtime.store import RunStore


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class ReviewFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = RunStore(Path(tmp.name) / "lab.sqlite")
        self.addCleanup(self.store.close)
        self.clock = _Clock()
        self.service = ReviewService(self.store, clock=self.clock)

    def _open(self, task_id: str = "tl_f7_two_eth_plans", run_id: str = "r1", policy: str = "on_ambiguity"):
        return run_task(
            store=self.store, task_id=task_id, model=RuleAgentStub(), run_id=run_id,
            review_policy=policy, service=self.service,
        )

    def _account(self, run_id: str = "r1") -> SimulationGateway:
        gw = SimulationGateway(self.store, account_id=f"acct_{run_id}")
        gw.load()
        return gw

    def test_review_is_held_and_worker_released(self):
        tr = self._open()
        self.assertEqual(tr.status, "waiting_review")
        self.assertEqual(self._account().submit_count, 0)
        self.assertEqual(self.store.get_run("r1")["status"], "waiting_review")
        card = self.store.get_review("card_r1")
        self.assertEqual((card["status"], card["revision"]), ("pending", 1))
        self.assertIn("multiple_candidate_plans", card["conflicts"])
        self.assertEqual({c["plan_id"] for c in card["candidates"]}, {"plan_eth_1", "plan_eth_2"})

    def test_wrong_agent_then_human_fix_then_recheck_then_complete(self):
        tr = self._open("tl_f7_missing_cost")
        self.assertFalse(agent_initial_score("tl_f7_missing_cost", tr).hard_pass)
        res = self.service.respond("card_r1", revision=1, action="modify", changes={"stop_loss": 1810})
        self.assertEqual(res.status, "modified")
        self.assertEqual(res.receipt["decision_version"], 2)  # human edit is a new decision version
        gw = self._account()
        plan = {p.plan_id: p for p in gw.plans()}["plan_a"]
        self.assertEqual((plan.stop_loss, gw.submit_count), (1810, 1))
        card = self.store.get_review("card_r1")
        self.assertEqual([p["source"] for p in card["proposal_history"]], ["agent", "human:cli"])
        self.assertTrue(self.store.get_run("r1")["payload"]["review"]["human_assisted"])

    def test_account_change_makes_approval_stale(self):
        self._open()
        SimulationGateway(self.store, account_id="acct_r1").external_update("plan_eth_1", stop_loss=1660)
        stale = self.service.respond("card_r1", revision=1, action="modify",
                                     changes={"plan_id": "plan_eth_2", "stop_loss": 1900})
        self.assertEqual((stale.status, stale.new_revision), ("stale_state", 2))
        self.assertEqual(self._account().submit_count, 0)
        old = self.service.respond("card_r1", revision=1, action="confirm")
        self.assertEqual(old.status, "stale_revision")
        ok = self.service.respond("card_r1", revision=2, action="modify",
                                  changes={"plan_id": "plan_eth_2", "stop_loss": 1900})
        self.assertEqual(ok.status, "modified")
        self.assertEqual(ok.decision["expected_state_version"], 3)

    def test_human_edit_is_revalidated(self):
        self._open()
        res = self.service.respond("card_r1", revision=1, action="modify",
                                   changes={"plan_id": "plan_eth_2", "stop_loss": 1500})
        self.assertEqual((res.status, res.errors), ("revalidation_failed", ["risk_stop_widened"]))
        self.assertEqual(self.store.get_review("card_r1")["status"], "pending")
        self.assertEqual(self._account().submit_count, 0)

    def test_only_allowed_fields(self):
        self._open()
        self.assertEqual(
            self.service.respond("card_r1", revision=1, action="modify", changes={"take_profit": 9}).status,
            "field_not_editable",
        )
        self.assertEqual(self.service.respond("card_r1", revision=1, action="modify").status, "invalid_action")
        self.assertEqual(self.service.respond("card_r1", revision=1, action="confirm").status, "nothing_to_confirm")

    def test_responses_are_idempotent(self):
        self._open()
        args = dict(revision=1, action="modify", changes={"plan_id": "plan_eth_2", "stop_loss": 1900})
        first = self.service.respond("card_r1", response_id="x1", **args)
        again = self.service.respond("card_r1", response_id="x1", **args)
        self.assertTrue(again.reused)
        self.assertEqual(again.receipt, first.receipt)
        mismatch = self.service.respond("card_r1", response_id="x1", revision=1, action="reject")
        self.assertEqual(mismatch.status, "response_payload_mismatch")
        late = self.service.respond("card_r1", revision=1, action="reject")
        self.assertEqual(late.status, "already_resolved")
        self.assertEqual(self._account().submit_count, 1)

    def test_expired_card(self):
        self._open()
        self.clock.now += timedelta(hours=25)
        res = self.service.respond("card_r1", revision=1, action="reject")
        self.assertEqual(res.status, "expired")
        self.assertEqual(self.store.get_run("r1")["status"], "timed_out")
        self.assertEqual(self._account().submit_count, 0)

    def test_fenced_run_cannot_be_revived_by_approval(self):
        self._open()
        self.store.put_fence(run_id="r1", reason="cancelled:operator")
        res = self.service.respond("card_r1", revision=1, action="modify",
                                   changes={"plan_id": "plan_eth_2", "stop_loss": 1900})
        self.assertEqual(res.status, "run_fenced")
        self.assertEqual(self.store.get_review("card_r1")["status"], "closed_fenced")
        self.assertEqual(self._account().submit_count, 0)
        with self.assertRaises(ValueError):
            self._open(run_id="r1b") if False else self.service.open_card(
                run_id="r1", task_id="t", account_id="acct_r1", proposal={},
                bundle=None, review_policy="on_ambiguity",  # type: ignore[arg-type]
            )

    def test_always_holds_confident_update_disabled_commits(self):
        held = self._open("tl_f0_move_sl_plan_a", run_id="a1", policy="always")
        self.assertEqual(held.status, "waiting_review")
        self.assertEqual(self._account("a1").submit_count, 0)
        direct = self._open("tl_f0_move_sl_plan_a", run_id="d1", policy="disabled")
        self.assertIsNone(direct.card)
        self.assertEqual(self._account("d1").submit_count, 1)

    def test_second_decision_after_commit_is_rejected(self):
        tr = self._open("tl_f0_move_sl_plan_a", run_id="s1", policy="on_ambiguity")
        self.assertEqual(self._account("s1").submit_count, 1)
        out = tr.environment.handle_tool(
            "submit_decision", {"kind": "review", "evidence_refs": ["message:C"]}
        )
        self.assertEqual(out.error_code, "decision_already_submitted")
        self.assertEqual(tr.environment.last_decision().kind, DecisionKind.UPDATE)
        self.assertIsNone(tr.environment.pending_proposal)

    def test_db_compare_and_swap_blocks_lost_update(self):
        self._open("tl_f0_move_sl_plan_a", run_id="c1", policy="always")
        a = SimulationGateway(self.store, account_id="acct_c1")
        a.load()
        SimulationGateway(self.store, account_id="acct_c1").external_update("plan_btc", stop_loss=101000)
        receipt = a.commit(
            run_id="c1",
            decision=SubmittedDecision(kind=DecisionKind.UPDATE, referenced_plan_id="plan_a",
                                       proposed_changes={"stop_loss": 1800}, evidence_refs=("m",)),
            expected_state_version=a.state_version,
        )
        self.assertFalse(receipt.applied)
        self.assertTrue(receipt.error_code.startswith("stale_state_version:db"))
        fresh = self._account("c1")
        self.assertEqual({p.plan_id: p.stop_loss for p in fresh.plans()}["plan_btc"], 101000)
        self.assertEqual(fresh.submit_count, 0)


class BatchEvalTests(unittest.TestCase):
    def test_policies_and_reviewers(self):
        s = run_eval(model_kind="stub")["summary"]
        self.assertEqual(s["disabled|-"]["final_completion_rate"], 0.5)
        self.assertEqual(s["disabled|-"]["assisted_success"], 0)
        amb = s["on_ambiguity|oracle_sim"]
        self.assertEqual((amb["auto_success"], amb["assisted_success"], amb["wrong_mutations"]), (3, 2, 1))
        full = s["always|oracle_sim"]
        self.assertEqual((full["final_completion_rate"], full["wrong_mutations"], full["unneeded_reviews"]), (1.0, 0, 3))
        self.assertEqual(s["always|rubber_stamp"]["final_completion_rate"], 0.5)
        self.assertGreater(full["human_minutes"], amb["human_minutes"])
        self.assertEqual(amb["stale_rebases"], 1)


class CliTests(unittest.TestCase):
    def test_run_show_modify(self):
        with tempfile.TemporaryDirectory() as d:
            db = ["--db", str(Path(d) / "lab.sqlite")]

            def call(*argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = cli.main([*db, *argv])
                return code, buf.getvalue()

            code, out = call("run", "--task", "tl_f7_two_eth_plans", "--run-id", "c")
            self.assertEqual(json.loads(out)["status"], "waiting_review")
            code, out = call("review", "show", "card_c")
            self.assertIn("审核卡 card_c", out)
            code, out = call("review", "modify", "card_c", "--revision", "1",
                             "--set", "plan_id=plan_eth_2", "--set", "stop_loss=1900")
            self.assertEqual((code, json.loads(out)["status"]), (0, "modified"))


if __name__ == "__main__":
    unittest.main()
