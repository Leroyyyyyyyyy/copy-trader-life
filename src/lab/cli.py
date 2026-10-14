"""Lab CLI: run a task, review held proposals, inspect/drift accounts, evaluate.

Examples:
  python -m src.lab.cli run --task tl_f7_two_eth_plans --review-policy on_ambiguity
  python -m src.lab.cli review list
  python -m src.lab.cli review show card_<run_id>
  python -m src.lab.cli review modify card_<run_id> --revision 1 --set plan_id=plan_eth_2 --set stop_loss=1900
  python -m src.lab.cli account drift --account acct_<run_id> --plan plan_eth_1 --stop-loss 1660
  python -m src.lab.cli evaluate --model stub
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from src.lab.execution.simulation import SimulationGateway
from src.lab.interaction.coordinator import run_task
from src.lab.interaction.review import ReviewService
from src.lab.runtime.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = REPO_ROOT / "data" / "lab" / "lab.sqlite"


def _print(obj: Any, as_json: bool = True) -> None:
    if as_json:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")
    else:
        sys.stdout.write(str(obj) + "\n")


def render_card(card: dict[str, Any]) -> str:
    p = card["proposal"]
    lines = [
        f"审核卡 {card['card_id']}  revision={card['revision']}  status={card['status']}",
        f"run={card['run_id']}  task={card['task_id']}  account={card['account_id']}  "
        f"state_version={card['state_version']}  expires_at={card['expires_at']}",
        "",
        "原消息：",
        *[f"  [{m['message_id']}] {m['text']}" for m in card["messages"]],
        "",
        f"拟议动作（v{p['version']}，来源 {p['source']}）：kind={p['kind']} plan_id={p.get('plan_id')} "
        f"stop_loss={p.get('stop_loss')}",
        f"  理由：{p.get('reason') or '-'}   证据：{', '.join(p.get('evidence_refs') or []) or '-'}",
        "字段来源：",
        *[f"  {k}: {v}" for k, v in card["field_sources"].items()],
        f"候选计划：{[(c['plan_id'], c['symbol'], c['cost_basis'], c['stop_loss']) for c in card['candidates']] or '-'}",
        f"缺失：{card['missing'] or '-'}   冲突：{card['conflicts'] or '-'}",
        f"模拟影响：{card['simulated_impact']}",
        "",
        "可做操作：confirm / reject / modify --set plan_id=... --set stop_loss=...（只允许这两个字段）",
    ]
    if card.get("resolution"):
        lines += ["", f"结论：{card['resolution']}"]
    return "\n".join(lines)


def _changes(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--set expects key=value, got {pair!r}")
        out[key.strip()] = value.strip()
    return out


def _model(kind: str, name: str | None) -> Any:
    if kind == "stub":
        from src.lab.experiments.f7_review import RuleAgentStub

        return RuleAgentStub()
    from src.lab.models.provider import DeepSeekModelAdapter

    return DeepSeekModelAdapter(model=name, timeout_sec=90.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.lab.cli", description="Tradelife lab CLI")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run one agent task")
    run_p.add_argument("--task", required=True)
    run_p.add_argument("--model", choices=("stub", "deepseek"), default="stub")
    run_p.add_argument("--model-name", default=None)
    run_p.add_argument("--review-policy", choices=("disabled", "on_ambiguity", "always"), default="on_ambiguity")
    run_p.add_argument("--run-id", default=None)

    rev = sub.add_parser("review", help="list / show / answer review cards")
    rsub = rev.add_subparsers(dest="rcmd", required=True)
    ls = rsub.add_parser("list")
    ls.add_argument("--status", default=None)
    show = rsub.add_parser("show")
    show.add_argument("card_id")
    show.add_argument("--json", action="store_true")
    for name in ("confirm", "reject", "modify"):
        a = rsub.add_parser(name)
        a.add_argument("card_id")
        a.add_argument("--revision", type=int, required=True)
        a.add_argument("--reviewer", default="cli")
        a.add_argument("--response-id", default=None)
        a.add_argument("--reason", default="")
        if name == "modify":
            a.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE")

    acct = sub.add_parser("account", help="inspect or change a simulated account")
    asub = acct.add_subparsers(dest="acmd", required=True)
    ashow = asub.add_parser("show")
    ashow.add_argument("--account", required=True)
    drift = asub.add_parser("drift", help="external stop change (makes open approvals stale)")
    drift.add_argument("--account", required=True)
    drift.add_argument("--plan", required=True)
    drift.add_argument("--stop-loss", type=float, required=True)

    ev = sub.add_parser("evaluate", help="F7 review-policy batch eval")
    ev.add_argument("--model", choices=("stub", "deepseek"), default="stub")
    ev.add_argument("--model-name", default=None)
    ev.add_argument("--repeats", type=int, default=1)
    ev.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.cmd == "evaluate":
        from src.lab.experiments.f7_review import main as eval_main

        extra = ["--model", args.model, "--repeats", str(args.repeats)]
        if args.model_name:
            extra += ["--model-name", args.model_name]
        if args.out:
            extra += ["--out", str(args.out)]
        return eval_main(extra)

    store = RunStore(args.db)
    try:
        service = ReviewService(store)
        if args.cmd == "run":
            run_id = args.run_id or f"cli_{uuid.uuid4().hex[:8]}"
            tr = run_task(
                store=store,
                task_id=args.task,
                model=_model(args.model, args.model_name),
                run_id=run_id,
                review_policy=args.review_policy,
                service=service,
                model_timeout_sec=120.0 if args.model == "deepseek" else None,
            )
            _print(
                {
                    "run_id": tr.run_id,
                    "account_id": tr.account_id,
                    "status": tr.status,
                    "terminal_reason": tr.result.terminal_reason,
                    "card_id": tr.card["card_id"] if tr.card else None,
                    "card_revision": tr.card["revision"] if tr.card else None,
                    "submit_count": tr.result.final_snapshot.submit_count,
                }
            )
            return 0
        if args.cmd == "review":
            if args.rcmd == "list":
                _print(
                    [
                        {k: c[k] for k in ("card_id", "status", "revision", "task_id", "state_version", "expires_at")}
                        for c in store.list_reviews(args.status)
                    ]
                )
                return 0
            if args.rcmd == "show":
                card = store.get_review(args.card_id)
                if card is None:
                    _print({"error": "unknown_card"})
                    return 1
                _print(card if args.json else render_card(card), as_json=args.json)
                return 0
            res = service.respond(
                args.card_id,
                revision=args.revision,
                action=args.rcmd,
                changes=_changes(getattr(args, "set", [])),
                reviewer=args.reviewer,
                response_id=args.response_id,
                reason=args.reason,
            )
            _print(res.to_dict())
            return 0 if res.committed else 2
        if args.cmd == "account":
            gw = SimulationGateway(store, account_id=args.account)
            if args.acmd == "drift":
                if not gw.load():
                    _print({"error": "unknown_account"})
                    return 1
                version = gw.external_update(args.plan, stop_loss=args.stop_loss)
                _print({"account_id": args.account, "state_version": version})
                return 0
            if not gw.load():
                _print({"error": "unknown_account"})
                return 1
            _print(gw.snapshot())
            return 0
    finally:
        store.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
