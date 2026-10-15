"""One month of real POPIGOGO posts, in order, through the lab agent and the rule parser.

Agent path per post: EventInbox dedupe -> DeepSeek in the harness (last 6 posts as
context, current positions of ONE persisted simulated account) -> ToolPolicy gate
-> commit (open requires a stop) or hold for review (on_ambiguity; cards are left
pending to measure human workload). Rule path: `src.parser.parse_message` with
the channel config, as the paper engine would parse it.

No labels exist for these posts, so nothing is scored as right or wrong here;
the report lists what each path did and where they disagree. No historical
prices: fills, stop hits and P&L are not simulated. Charts are not read; the
agent only sees that a post had one.

Resumable: rows are appended to rows.jsonl and the inbox skips posts already run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from src.lab.domain import (
    EnvironmentKind,
    MessageView,
    ObservationBundle,
    RunBudget,
    TaskSpec,
)
from src.lab.environments.plan_simulation import PlanSimulationEnvironment
from src.lab.execution.simulation import SimulationGateway
from src.lab.harness.loop import AgentHarness
from src.lab.interaction.review import ReviewService
from src.lab.models.provider import REPO_ROOT, DeepSeekModelAdapter
from src.lab.runtime.inbox import EventInbox
from src.lab.runtime.store import RunStore
from src.lab.tools.policy import ToolPolicy
from src.parser import parse_message

CHANNEL = "POPIGOGO"
ACCOUNT = "acct_popi_30d"
DEFAULT_POSTS = REPO_ROOT / "data" / "lab" / "popigogo_30d.json"
DEFAULT_DIR = REPO_ROOT / "data" / "lab" / "month_replay"
CONTEXT = 6
OBJECTIVE = (
    "You follow the POPIGOGO channel for this simulated account. Decide what to do about the latest "
    "message only: open a position, change a stop on an existing plan, ignore it, or submit review. "
    "Earlier messages are context. Commit at most once."
)
NOTE = (
    "Replay of the public channel, 2026-09-14..2026-10-14. No live market data. Messages marked with "
    "image_refs carried a chart image that you cannot see. Simulated account."
)


def rule_view(post: dict[str, Any], ch: dict[str, Any]) -> dict[str, Any]:
    sig = parse_message(channel=CHANNEL, message_id=post["id"], date=post["date"], text=post["text"],
                        default_symbol=ch.get("default_symbol"), other_assets=ch.get("other_assets"))
    boost = float(ch.get("confidence_boost") or 0)
    conf = sig.confidence if sig.skip_reason else max(0.0, min(0.99, sig.confidence + boost))
    acts = (not sig.skip_reason and sig.intent.value != "ignore"
            and conf >= float(ch.get("min_confidence") or 0))
    return {"acts": bool(acts), "side": sig.side.value, "intent": sig.intent.value, "symbol": sig.symbol,
            "confidence": round(conf, 2), "skip": sig.skip_reason, "stop_loss": sig.stop_loss,
            "take_profit": sig.take_profit, "trigger": getattr(sig, "trigger_price", None)}


def _bundle(posts: list[dict[str, Any]], i: int, gateway: SimulationGateway) -> ObservationBundle:
    window = [p for p in posts[max(0, i - CONTEXT): i + 1] if p["text"]]
    return ObservationBundle(
        bundle_id=f"popi_{posts[i]['id']}",
        messages=tuple(
            MessageView(message_id=str(p["id"]), channel=CHANNEL, visible_at=p["date"], text=p["text"],
                        image_refs=("chart",) if p["has_photo"] else ())
            for p in window
        ),
        plans=gateway.plans(),
        account_note=NOTE,
    )


def run(posts_path: Path, out_dir: Path, limit: int | None, temperature: float) -> Path:
    posts = json.loads(posts_path.read_text(encoding="utf-8"))
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    ch = next(c for c in cfg["channels"] if c["username"] == CHANNEL)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    store = RunStore(out_dir / "lab.sqlite")
    inbox = EventInbox(store)
    service = ReviewService(store)
    gateway = SimulationGateway(store, account_id=ACCOUNT)
    if not gateway.load():
        gateway.reset((), 1)
    model = DeepSeekModelAdapter(temperature=temperature, timeout_sec=90.0)
    done = 0
    try:
        for i, post in enumerate(posts):
            if limit is not None and done >= limit:
                break
            if not post["text"]:
                continue
            delivery = inbox.deliver(f"{CHANNEL}:{post['id']}", {"text": post["text"]})
            if not delivery.accepted:
                continue  # already processed in an earlier (interrupted) run
            run_id = delivery.run_id
            trace_id = "trace_" + hashlib.sha256(f"{CHANNEL}:{post['id']}".encode()).hexdigest()[:16]
            gateway.load()
            before = {p.plan_id: p.to_dict() for p in gateway.plans()}
            env = PlanSimulationEnvironment(
                _bundle(posts, i, gateway), gateway, run_id=run_id,
                review_policy="on_ambiguity", allow_open=True, reset_account=False,
            )
            task = TaskSpec(
                task_id=f"popi_{post['id']}", family_id="month_replay", split="popi_2026_09_10",
                environment_kind=EnvironmentKind.PLAN_SIMULATION, objective=OBJECTIVE,
                observation_bundle_id=f"popi_{post['id']}", visible_at=post["date"],
                initial_snapshot_id=f"{ACCOUNT}@v{gateway.state_version}",
            )
            harness = AgentHarness(
                store=store, model=model, environment=env,
                budget=RunBudget(max_model_calls=4, max_tool_calls=6),
                policy=ToolPolicy(mode="enforce", plans=env.plan_views), model_timeout_sec=120.0,
            )
            t0 = time.perf_counter()
            result = harness.run(task, run_id=run_id, trace_id=trace_id)
            ms = (time.perf_counter() - t0) * 1000.0
            card = None
            if env.pending_proposal is not None and store.get_fence(run_id) is None:
                card = service.open_card(run_id=run_id, task_id=task.task_id, account_id=ACCOUNT,
                                         proposal=env.pending_proposal, bundle=env.bundle,
                                         review_policy="on_ambiguity", trace_id=trace_id)
            gateway.load()
            after = {p.plan_id: p.to_dict() for p in gateway.plans()}
            d = result.decision
            row = {
                "id": post["id"], "date": post["date"], "text": post["text"], "has_photo": post["has_photo"],
                "rule": rule_view(post, ch),
                "agent": {
                    "status": result.status.value,
                    "terminal_reason": result.terminal_reason,
                    "decision": None if d is None else {"kind": d.kind.value, "plan": d.referenced_plan_id,
                                                        "changes": d.proposed_changes},
                    "held_for_review": card is not None,
                    "proposal": env.pending_proposal,
                    "opened": [after[k] for k in after if k not in before],
                    "changed": [{"plan_id": k, "before": before[k]["stop_loss"], "after": after[k]["stop_loss"]}
                                for k in after if k in before and before[k]["stop_loss"] != after[k]["stop_loss"]],
                    "tools": [(e["payload"].get("name"), e["payload"].get("status"), e["payload"].get("error_code"))
                              for e in result.events if e["kind"] == "tool_result"],
                    "policy_violations": sum(1 for e in result.events if e["kind"] == "policy_violation"),
                    "summary": next((e["payload"].get("summary") for e in result.events if e["kind"] == "final"), None),
                    "usage": {"in": result.usage.input_tokens, "out": result.usage.output_tokens,
                              "calls": result.usage.model_calls},
                    "latency_ms": round(ms, 1),
                },
                "run_id": run_id, "trace_id": trace_id,
            }
            with rows_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            done += 1
            print(f"[{done}] #{post['id']} rule={'ACT' if row['rule']['acts'] else '-'} "
                  f"agent={row['agent']['decision']['kind'] if row['agent']['decision'] else None}"
                  f"{' HOLD' if card else ''} {ms/1000:.1f}s", flush=True)
    finally:
        store.close()
    return rows_path


def summarize(rows_path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    agent_kind = Counter((r["agent"]["decision"] or {}).get("kind", "none") for r in rows)
    agent_acts = [r for r in rows if r["agent"]["opened"] or r["agent"]["changed"]]
    held = [r for r in rows if r["agent"]["held_for_review"]]
    rule_acts = [r for r in rows if r["rule"]["acts"]]
    both = [r for r in rows if r["rule"]["acts"] and (r["agent"]["opened"] or r["agent"]["changed"])]
    rule_only = [r for r in rows if r["rule"]["acts"] and not (r["agent"]["opened"] or r["agent"]["changed"])]
    agent_only = [r for r in rows if not r["rule"]["acts"] and (r["agent"]["opened"] or r["agent"]["changed"])]
    rule_no_stop = [r for r in rule_acts if r["rule"]["intent"] in {"market", "limit", "conditional"} and r["rule"]["stop_loss"] is None]
    return {
        "posts_run": len(rows),
        "with_chart": sum(r["has_photo"] for r in rows),
        "rule": {"acts": len(rule_acts), "by_intent": Counter(r["rule"]["intent"] for r in rule_acts),
                 "entries_without_stop": len(rule_no_stop)},
        "agent": {"decisions": agent_kind, "state_changes": len(agent_acts),
                  "opened": sum(len(r["agent"]["opened"]) for r in rows),
                  "stop_changes": sum(len(r["agent"]["changed"]) for r in rows),
                  "held_for_review": len(held),
                  "policy_violations": sum(r["agent"]["policy_violations"] for r in rows),
                  "non_success_runs": Counter(r["agent"]["status"] for r in rows if r["agent"]["status"] != "succeeded")},
        "agreement": {"both_act": len(both), "rule_only": len(rule_only), "agent_only": len(agent_only),
                      "neither": len(rows) - len(both) - len(rule_only) - len(agent_only)},
        "tokens": {"in": sum(r["agent"]["usage"]["in"] for r in rows),
                   "out": sum(r["agent"]["usage"]["out"] for r in rows)},
        "minutes": round(sum(r["agent"]["latency_ms"] for r in rows) / 60000.0, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Month replay of POPIGOGO through the lab agent")
    ap.add_argument("--posts", type=Path, default=DEFAULT_POSTS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args(argv)
    rows_path = args.out_dir / "rows.jsonl"
    if not args.summary_only:
        rows_path = run(args.posts, args.out_dir, args.limit, args.temperature)
    sys.stdout.write(json.dumps(summarize(rows_path), ensure_ascii=False, indent=2, default=str) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
