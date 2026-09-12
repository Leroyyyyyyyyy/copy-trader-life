from __future__ import annotations

import json
import unittest
from pathlib import Path

from src.lab.domain import (
    DecisionKind,
    FinalEnvironmentSnapshot,
    PlanStatus,
    PlanView,
    SubmittedDecision,
    TraceSummary,
)
from src.lab.evals.loader import (
    LabelLeakError,
    load_agent_view,
    load_for_eval,
    load_observation,
)
from src.lab.evals.runner import score_payload
from src.lab.evals.verifiers import verify_plan_decision

ROOT = Path(__file__).resolve().parents[2]
HAND = ROOT / "experiments" / "fixtures" / "handwritten"


def _plan(
    plan_id: str,
    *,
    symbol: str = "ETH",
    status: PlanStatus = PlanStatus.FILLED,
    stop_loss: float | None = 1650,
    cost: float | None = 1800,
    version: int = 3,
) -> PlanView:
    return PlanView(
        plan_id=plan_id,
        symbol=symbol,
        status=status,
        side="buy",
        cost_basis=cost,
        stop_loss=stop_loss,
        take_profit=2200 if symbol == "ETH" else 120000,
        state_version=version,
    )


class LoaderIsolationTests(unittest.TestCase):
    def test_agent_view_has_no_gold_fields(self):
        task, obs = load_agent_view("tl_f0_move_sl_plan_a")
        self.assertEqual(task.task_id, "tl_f0_move_sl_plan_a")
        self.assertEqual(obs.bundle_id, "tl_f0_move_sl_plan_a")
        self.assertNotIn("acceptable_outcomes", task.to_dict())

    def test_labels_load_separately(self):
        _, _, spec = load_for_eval("tl_f0_move_sl_plan_a")
        self.assertEqual(spec.acceptable_outcomes[0].stop_loss, 1800)
        self.assertEqual(spec.acceptable_outcomes[0].referenced_plan_id, "plan_a")

    def test_observation_rejects_embedded_labels(self):
        bad = {
            "bundle_id": "x",
            "messages": [],
            "plans": [],
            "acceptable_outcomes": [{"kind": "update"}],
        }
        path = ROOT / "experiments" / "fixtures" / "observations" / "_tmp_bad.json"
        try:
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(LabelLeakError):
                load_observation("_tmp_bad")
        finally:
            if path.exists():
                path.unlink()


class VerifierMoveSlTests(unittest.TestCase):
    def setUp(self):
        _, _, self.spec = load_for_eval("tl_f0_move_sl_plan_a")
        self.initial = self.spec.initial_plans

    def test_correct_update_passes(self):
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C", "plan:plan_a"),
        )
        final = FinalEnvironmentSnapshot(
            plans=(
                _plan("plan_a", stop_loss=1800, version=4),
                _plan(
                    "plan_btc",
                    symbol="BTC",
                    stop_loss=100000,
                    cost=108000,
                    version=2,
                ),
            ),
            submit_count=1,
            state_version=4,
        )
        score = verify_plan_decision(
            task_id="tl_f0_move_sl_plan_a",
            run_id="t_correct",
            eval_spec=self.spec,
            decision=decision,
            final_snapshot=final,
            initial_plans=self.initial,
        )
        self.assertTrue(score.hard_pass)
        self.assertEqual(score.terminal_reason, "passed")
        self.assertIn("plan_a", score.matched_outcome or "")

    def test_wrong_plan_fails(self):
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_btc",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C",),
        )
        final = FinalEnvironmentSnapshot(
            plans=(
                _plan("plan_a", stop_loss=1650, version=3),
                _plan(
                    "plan_btc",
                    symbol="BTC",
                    stop_loss=1800,
                    cost=108000,
                    version=3,
                ),
            ),
            submit_count=1,
        )
        score = verify_plan_decision(
            task_id="tl_f0_move_sl_plan_a",
            run_id="t_wrong",
            eval_spec=self.spec,
            decision=decision,
            final_snapshot=final,
            initial_plans=self.initial,
        )
        self.assertFalse(score.hard_pass)
        joined = " ".join(score.semantic_errors)
        self.assertTrue("wrong_plan" in joined or "non_target_plan_changed" in joined)

    def test_format_ok_verbal_success_fails(self):
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C",),
            claimed_success=True,
        )
        final = FinalEnvironmentSnapshot(plans=self.initial, submit_count=0)
        score = verify_plan_decision(
            task_id="tl_f0_move_sl_plan_a",
            run_id="t_verbal",
            eval_spec=self.spec,
            decision=decision,
            final_snapshot=final,
            initial_plans=self.initial,
            trace=TraceSummary(claimed_done_without_submit=True),
        )
        self.assertFalse(score.hard_pass)
        joined = " ".join(score.semantic_errors + score.protocol_errors)
        self.assertTrue(
            "claimed_success" in joined or "format_ok_but_no_state_change" in joined
        )

    def test_missing_evidence_is_protocol_fail(self):
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_a",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=(),
        )
        final = FinalEnvironmentSnapshot(
            plans=(
                _plan("plan_a", stop_loss=1800, version=4),
                _plan(
                    "plan_btc",
                    symbol="BTC",
                    stop_loss=100000,
                    cost=108000,
                    version=2,
                ),
            ),
            submit_count=1,
        )
        score = verify_plan_decision(
            task_id="tl_f0_move_sl_plan_a",
            run_id="t_no_ev",
            eval_spec=self.spec,
            decision=decision,
            final_snapshot=final,
            initial_plans=self.initial,
        )
        self.assertFalse(score.hard_pass)
        self.assertTrue(any("insufficient_evidence" in e for e in score.protocol_errors))


class FixtureVariantTests(unittest.TestCase):
    def test_ambiguous_review_passes(self):
        _, _, spec = load_for_eval("tl_f0_ambiguous_eth")
        decision = SubmittedDecision(
            kind=DecisionKind.REVIEW,
            evidence_refs=("message:C", "plan:plan_eth_1", "plan:plan_eth_2"),
        )
        final = FinalEnvironmentSnapshot(plans=spec.initial_plans, submit_count=1)
        score = verify_plan_decision(
            task_id="tl_f0_ambiguous_eth",
            run_id="t_review",
            eval_spec=spec,
            decision=decision,
            final_snapshot=final,
        )
        self.assertTrue(score.hard_pass)

    def test_ambiguous_silent_update_fails(self):
        _, _, spec = load_for_eval("tl_f0_ambiguous_eth")
        decision = SubmittedDecision(
            kind=DecisionKind.UPDATE,
            referenced_plan_id="plan_eth_1",
            proposed_changes={"stop_loss": 1800},
            evidence_refs=("message:C",),
        )
        changed = (
            _plan("plan_eth_1", stop_loss=1800, cost=1800, version=3),
            _plan("plan_eth_2", stop_loss=1750, cost=1900, version=2),
        )
        final = FinalEnvironmentSnapshot(plans=changed, submit_count=1)
        score = verify_plan_decision(
            task_id="tl_f0_ambiguous_eth",
            run_id="t_guess",
            eval_spec=spec,
            decision=decision,
            final_snapshot=final,
        )
        self.assertFalse(score.hard_pass)

    def test_closed_plan_ignore_passes(self):
        _, _, spec = load_for_eval("tl_f0_closed_plan")
        decision = SubmittedDecision(
            kind=DecisionKind.IGNORE,
            referenced_plan_id="plan_a",
            evidence_refs=("message:C", "plan:plan_a"),
        )
        final = FinalEnvironmentSnapshot(plans=spec.initial_plans, submit_count=1)
        score = verify_plan_decision(
            task_id="tl_f0_closed_plan",
            run_id="t_ignore",
            eval_spec=spec,
            decision=decision,
            final_snapshot=final,
        )
        self.assertTrue(score.hard_pass)


class HandwrittenFixtureTests(unittest.TestCase):
    def test_handwritten_correct_passes(self):
        payload = json.loads(
            (HAND / "tl_f0_move_sl_plan_a_correct.json").read_text(encoding="utf-8")
        )
        result = score_payload(payload)
        self.assertTrue(result["hard_pass"])

    def test_handwritten_wrong_plan_fails(self):
        payload = json.loads(
            (HAND / "tl_f0_move_sl_plan_a_wrong_plan.json").read_text(encoding="utf-8")
        )
        result = score_payload(payload)
        self.assertFalse(result["hard_pass"])

    def test_handwritten_verbal_fails(self):
        payload = json.loads(
            (HAND / "tl_f0_move_sl_plan_a_verbal_only.json").read_text(encoding="utf-8")
        )
        result = score_payload(payload)
        self.assertFalse(result["hard_pass"])


if __name__ == "__main__":
    unittest.main()
