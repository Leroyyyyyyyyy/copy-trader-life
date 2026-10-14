"""Same real channel posts through two paths: the original rule parser and the lab agent.

Rule path: `src.parser.parse_message` with the channel's config (what the paper
engine acted on during the 2026-10-14 replay). Agent path: DeepSeek in the lab
harness with the policy gate, a simulated account, and an opt-in
`submit_new_order` tool that requires a stop. Both are scored against the same
hidden labels. One synthetic positive control checks the agent is not simply
refusing everything.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import yaml

from src.lab.domain import DecisionKind, FinalEnvironmentSnapshot, SubmittedDecision
from src.lab.evals.loader import load_agent_view, load_eval_spec
from src.lab.evals.verifiers import verify_plan_decision
from src.lab.interaction.coordinator import run_task
from src.lab.models.provider import REPO_ROOT, DeepSeekModelAdapter
from src.lab.runtime.store import RunStore
from src.parser import parse_message

TASKS = ("tl_real_8081_recap", "tl_real_8089_negation", "tl_real_8092_complaint", "tl_synth_explicit_short")


def rule_parser_view(task_id: str) -> dict[str, Any]:
    """What the original parser makes of the latest message (before engine risk sizing)."""
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    ch = next(c for c in cfg["channels"] if c["username"] == "POPIGOGO")
    _, obs = load_agent_view(task_id)
    last = obs.messages[-1]
    msg_id = int(last.message_id) if last.message_id.isdigit() else 0
    sig = parse_message(channel="POPIGOGO", message_id=msg_id, date=last.visible_at, text=last.text,
                        default_symbol=ch.get("default_symbol"))
    boost = float(ch.get("confidence_boost") or 0)
    conf = sig.confidence if sig.skip_reason else max(0.0, min(0.99, sig.confidence + boost))
    acts = not sig.skip_reason and sig.intent.value != "ignore" and conf >= float(ch.get("min_confidence") or 0)
    return {
        "side": sig.side.value, "intent": sig.intent.value, "symbol": sig.symbol,
        "confidence": round(conf, 2), "skip": sig.skip_reason,
        "stop_loss": sig.stop_loss, "take_profit": sig.take_profit,
        "would_act": bool(acts),
    }


def rule_parser_pass(task_id: str, view: dict[str, Any]) -> bool:
    want = load_eval_spec(task_id).acceptable_outcomes
    if not view["would_act"]:
        return any(o.kind in {DecisionKind.IGNORE, DecisionKind.REVIEW} for o in want)
    for o in want:
        if o.kind == DecisionKind.OPEN and view["intent"] == "market" and view["symbol"] == o.symbol \
                and view["side"] == o.side and view["stop_loss"] is not None:
            return True
    return False


def run_agent(task_id: str, rep: int, model_name: Optional[str], temperature: float) -> dict[str, Any]:
    tmp = tempfile.TemporaryDirectory()
    store = RunStore(Path(tmp.name) / "lab.sqlite")
    try:
        t0 = time.perf_counter()
        tr = run_task(store=store, task_id=task_id,
                      model=DeepSeekModelAdapter(model=model_name, temperature=temperature, timeout_sec=90.0),
                      run_id=f"replay_{task_id}_{rep}", review_policy="disabled", allow_open=True,
                      model_timeout_sec=120.0)
        ms = (time.perf_counter() - t0) * 1000.0
        snap: FinalEnvironmentSnapshot = tr.result.final_snapshot
        decision = tr.result.decision or SubmittedDecision(kind=DecisionKind.IGNORE)
        score = verify_plan_decision(task_id=task_id, run_id=tr.run_id, eval_spec=load_eval_spec(task_id),
                                     decision=decision, final_snapshot=snap)
        tools = [(e["payload"].get("name"), e["payload"].get("status"), e["payload"].get("error_code"))
                 for e in tr.result.events if e["kind"] == "tool_result"]
        final = next((e["payload"] for e in tr.result.events if e["kind"] == "final"), {})
        return {
            "repeat": rep,
            "status": tr.result.status.value,
            "decision": {"kind": decision.kind.value, "plan": decision.referenced_plan_id,
                         "changes": decision.proposed_changes},
            "new_positions": [p.to_dict() for p in snap.plans if p.plan_id.startswith("plan_open_")],
            "hard_pass": score.hard_pass,
            "errors": score.protocol_errors + score.semantic_errors,
            "tools": tools,
            "summary": final.get("summary"),
            "usage": {"in": tr.result.usage.input_tokens, "out": tr.result.usage.output_tokens},
            "latency_ms": round(ms, 1),
        }
    finally:
        store.close()
        tmp.cleanup()


def run(repeats: int = 3, model_name: Optional[str] = None, temperature: float = 0.7) -> dict[str, Any]:
    rows = []
    for task_id in TASKS:
        rule = rule_parser_view(task_id)
        agent = [run_agent(task_id, r, model_name, temperature) for r in range(repeats)]
        _, obs = load_agent_view(task_id)
        rows.append({
            "task_id": task_id,
            "source": json.loads((REPO_ROOT / "experiments/fixtures/labels" / f"{task_id}.json").read_text())["source"],
            "message": obs.messages[-1].text,
            "rule_parser": {**rule, "pass": rule_parser_pass(task_id, rule)},
            "agent_pass": sum(a["hard_pass"] for a in agent),
            "agent_runs": agent,
        })
    return {
        "experiment": "real_replay_rule_vs_agent.v1",
        "model": model_name or "env:DEEPSEEK_LAB_MODEL|deepseek-flash",
        "temperature": temperature,
        "repeats": repeats,
        "review_policy": "disabled (agent decision is final, like the rule engine)",
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rule parser vs DeepSeek agent on replayed posts")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--model-name", default=None)
    ap.add_argument("--temperature", type=float, default=0.7)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    report = run(args.repeats, args.model_name, args.temperature)
    blob = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(blob + "\n", encoding="utf-8")
    sys.stdout.write(blob + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
