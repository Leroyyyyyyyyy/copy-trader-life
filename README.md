# Tradelife · Agent 学习与应用项目

用 Telegram 消息、图表和模拟交易任务学习 agent。**当前任务：实施 L0–L7，按 F0–F7 推进，完成后停在 L7。** 使用 coding agent 协助写代码，项目本人需要能独立解释设计、关键实现、实验和失败恢复，准备面试答辩。

## 项目入口

- **唯一详细设计**：[Agent 学习与实施手册](docs/agent_architecture_v2.md)。先读 [1.7 框架规格](docs/agent_architecture_v2.md#framework-l0-l7)与 [1.8 制作和答辩要求](docs/agent_architecture_v2.md#coding-and-defense)。
- **接手实施**：[handoff.md](handoff.md)，包含真实状态、下一批工作与可复制提示。
- README 维护入口与运行说明，handoff 维护进度，手册维护设计与验收；不复制成三份架构。

## 当前状态（2026-10-14）

| 部分 | 状态 |
|---|---|
| 原有 Telegram → parser → Engine → Hyperliquid/Solana 代码 | 已存在，是业务与测试基线 |
| L0–L7 模块、接口、实验、恢复、eval 与人工复核设计 | 已完成文档 |
| F0：`TaskSpec`/`EvalSpec`、冻结样例、单例 verifier | 已实现（离线，无 LLM/venue） |
| F1：共用 harness、stub、两环境、幂等 gateway | 已实现 |
| L2：ContextBuilder、阈值压缩、原始 trace 不变 | 已实现 |
| L3：SQLite memory、SkillRegistry、按需加载 | 已实现（F2 收口） |
| H2：两 stub 模型 × 两种观察的 2×2 | 已实现（协议 stub，非真实 LLM） |
| H3：Docker SandboxExecutor、程序模拟后单次提交 | 已实现；无 daemon 时 `run_program` 禁用 |
| L4：多角色编排、只读 fan-out、证据汇合 | 已实现（确定性角色；非 LangGraph） |
| L5：native / LangGraph WorkflowAdapter 对照 | 已实现；共用节点，checkpoint 不含账户 |
| L6：故障集、权限/风控闸门、取消与超时、结构化 trace | 已实现；DeepSeek 用于对抗输入对照，故障注入用确定性脚本模型 |
| L7：审核卡、CLI 复核、旧审批失效、审核策略批量 eval | 已实现；模拟审核员（非真人），DeepSeek 与规则桩各一份报告 |
| 当前状态 | **L0–L7 已全部实施**（F0–F7）。后续 L8–L11 不在本次范围 |
| L8–L11、SFT/RL、推理集群与模型发布 | 后续资料，不属于本次实施 |

F0–F7 实测：

```bash
.venv/bin/python -m unittest tests.lab.test_f0_verifier tests.lab.test_f1_harness tests.lab.test_l2_context tests.lab.test_l3_memory_skills tests.lab.test_h2_observation tests.lab.test_h3_sandbox tests.lab.test_l4_roles tests.lab.test_l5_workflows tests.lab.test_l6_faults tests.lab.test_l7_review tests.lab.test_e2e_cli tests.lab.test_real_replay -v
```

历史 testnet 记录见 handoff，不代表当前交易环境已验证。

## 实施方式

按手册 F0–F7 分批交付可运行增量，在 `src/lab/` 建立独立模拟实验框架，保留原有业务入口。每批提供真实检查结果、关键路径讲解、trace 与本人理解练习；代码通过和本人已掌握分别记录。

初期使用 stub 模型、SQLite/文件、只读或模拟工具；L4 用原生编排对照串行/并发，L5 用同一节点函数对照 native FSM 与 LangGraph。L7 先用 CLI 复核。H1/H2/H3、压缩、记忆与 Skills 实验都在手册中。Lab 的命令与依赖尚未做成独立 CLI，手册配置不是可运行入口。

评测选型：**DeepEval + 项目确定性 verifier**，详见手册 [2.3 节](docs/agent_architecture_v2.md#deepeval)。F0 已实现离线确定性 verifier；DeepEval 适配器仍待 harness 稳定后接入。

## 现有代码基线

```text
src/main.py / src/bot.py → src/parser.py → src/engine.py
                                             ├─ src/risk.py
                                             ├─ src/chart_reader.py
                                             ├─ src/hyperliquid_venue.py
                                             └─ src/solana_venue.py / src/gmgn.py
src/models.py：Signal / Plan       src/store.py：data/state.json
```

频道、仓位和风控配置以 `config.yaml`、`src/risk.py` 为准。现有代码有市场/限价/条件计划、图表价位读取、Hyperliquid TP/SL 与对账；Solana 路由优先使用已配置的 GMGN CLI，否则尝试 Jupiter。支持范围以代码为准，例如 Solana `execute` 当前不启用卖出。

## 安装和检查现有基线

Python 3.10+。以下安装现有依赖，不包括未来 lab/训练栈：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest tests.test_parser tests.test_risk -v
```

解析/风控单测不需要 Telegram 登录或交易凭证。现有全量命令为 `python -m unittest discover -s tests -v`；先检查当前测试的 mock/存储隔离，再执行并记录实测结果。

Lab F0–F7（离线，不改 `data/state.json`）：

```bash
source .venv/bin/activate   # macOS 若无 python，用 .venv/bin/python
python -m unittest tests.lab.test_f0_verifier tests.lab.test_f1_harness tests.lab.test_l2_context tests.lab.test_l3_memory_skills tests.lab.test_h2_observation tests.lab.test_h3_sandbox tests.lab.test_l4_roles tests.lab.test_l5_workflows tests.lab.test_l6_faults tests.lab.test_l7_review tests.lab.test_e2e_cli tests.lab.test_real_replay -v
python -m src.lab.evals.runner experiments/fixtures/handwritten/tl_f0_move_sl_plan_a_correct.json
python -m src.lab.experiments.h2_observation
python -m src.lab.experiments.f6_faults --part all --model stub
python -m src.lab.cli evaluate --model stub
```

L7 人工复核 CLI（默认库 `data/lab/lab.sqlite`，可用 `--db` 指定）：

```bash
python -m src.lab.cli run --task tl_f7_two_eth_plans --review-policy on_ambiguity   # 输出 run_id / card_id
python -m src.lab.cli review list
python -m src.lab.cli review show card_<run_id>
python -m src.lab.cli review modify card_<run_id> --revision 1 --set plan_id=plan_eth_2 --set stop_loss=1900
python -m src.lab.cli review confirm card_<run_id> --revision 1
python -m src.lab.cli review reject card_<run_id> --revision 1 --reason "..."
python -m src.lab.cli account show --account acct_<run_id>
python -m src.lab.cli account drift --account acct_<run_id> --plan plan_eth_1 --stop-loss 1660   # 模拟外部改动，使待审批变 stale
python -m src.lab.cli run --task tl_f0_move_sl_plan_a --event-id POPIGOGO:7003   # 经事件去重；run/trace id 由事件 id 派生
python -m src.lab.cli score <run_id>    # 用隐藏标签给最终结果判分（agent 或复核后的决定）
python -m src.lab.cli trace <run_id>    # 导出脱敏 trace（JSONL）并给出归因与阶段耗时
```

端到端（每条命令一个子进程、全新数据库；`--model deepseek` 会产生费用）：

```bash
python -m src.lab.experiments.e2e_cli --model stub
python -m src.lab.experiments.e2e_cli --model deepseek --out experiments/reports/e2e_deepseek.json
python -m src.lab.experiments.real_replay --repeats 3   # 规则解析器 vs DeepSeek，同一批回放消息
python -m src.lab.experiments.month_replay              # 近 30 天 POPIGOGO 逐条回放（需先抓取到 data/lab/popigogo_30d.json）
```

`run` 默认用规则桩 agent；加 `--model deepseek` 用真实模型。审核策略：`disabled`（agent 决定即提交）、`on_ambiguity`（agent 提交 review 时交人审）、`always`（所有提交都交人审）。

F6/F7 真实模型实验（会调用 DeepSeek API 并产生费用）：

```bash
python -m src.lab.experiments.f6_faults --part all --model deepseek --repeats 3 --out experiments/reports/f6_deepseek.json
python -m src.lab.cli evaluate --model deepseek --out experiments/reports/f7_deepseek.json
```

Lab 的 DeepSeek 适配器（`src/lab/models/provider.py`）只从 `.env` 读取 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、可选 `DEEPSEEK_LAB_MODEL`（默认 `deepseek-flash`）三行，不把整份 `.env` 载入进程环境。

图表读图（`src/chart_reader.py`）在引擎里用 `ocr_primary`：价位数字取自 tesseract OCR（`src/chart_ocr.py`，同一读数至少 3 个预处理变体一致），DeepSeek 视觉只用来确认哪个标签是入场、哪个是止损；止损必须是最外侧的红色标签且离视觉读数最近，否则该带图突破单不建计划（`chart_stop_unverified`）。需要本机安装 tesseract（macOS：`brew install tesseract`）；未安装时一律不采用读图价位。读图评测：`python -m src.lab.experiments.chart_eval`，留出集加 `--heldout experiments/fixtures/charts/popi_2026_h1_heldout_labels.json`（标准答案 `experiments/fixtures/charts/`，图片在被忽略的 `data/lab/popi_images/`）。

LLM 闸门（`src/llm_gate.py`）：规则解析器判出要下单/改单/平仓后、读图之前，DeepSeek 读这条消息和频道前 6 条，判断作者是在叫人交易（`enter_now` / `conditional_setup` / `adjust_exit` / `close`）还是在复盘、炫耀、评论（`not_actionable`）；方向、币种也要与解析器一致才放行，出错默认不放行。配置 `config.yaml` 的 `llm_gate: {enabled, on_error}`；模型取 `DEEPSEEK_GATE_MODEL`，否则同 lab。评测：`python -m src.lab.experiments.gate_eval --repeats 3`（标准答案 `experiments/fixtures/gate/`）。

H3 的 `run_program` 需要本机 Docker daemon 与 `python:3.12-alpine`。daemon 未运行时该工具不注册，不会退回宿主 `exec/eval`。L5 的 LangGraph adapter 需要 `langgraph`（见 `requirements.txt`）；native adapter 不依赖它。

观察与隐藏标签分目录：`experiments/fixtures/observations/` 与 `experiments/fixtures/labels/`。Agent 路径只应使用 `load_agent_view`，不要加载 labels。F1 共用循环在 `src/lab/harness/loop.py`。L2 压缩在 `src/lab/context/`。L3 记忆在 `src/lab/memory/`，技能在 `src/lab/skills/` 与 `experiments/skill_library/`。H2 观察策略在 `src/lab/environments/allowed_actions.py`，矩阵在 `src/lab/experiments/h2_observation.py`。H3 沙箱在 `src/lab/sandbox/`。L4 角色与编排在 `src/lab/agents/`。L5 共用节点与两套 adapter 在 `src/lab/workflows/`。L6 闸门在 `src/lab/tools/policy.py`，trace 脱敏/归因在 `src/lab/observability/`，取消与事件去重在 `src/lab/runtime/cancel.py`、`inbox.py`，故障集与对照在 `src/lab/experiments/f6_faults.py`，报告写入 `experiments/reports/`。L7 复核在 `src/lab/interaction/`（`review.py` 审核卡与响应校验，`coordinator.py` 运行并转审），CLI 在 `src/lab/cli.py`，审核策略批量评测在 `src/lab/experiments/f7_review.py`。

## 现有纸交易入口（非新 lab）

实时监听需要配置 Telegram。仅在 `.env` 不存在时按 `.env.example` 新建，已有配置不覆盖；填入 `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` 后首次登录会生成 session。视觉读取所需配置由 `src/chart_reader.py` 定义。

```bash
# 公开页面回放；不要求 Telegram 登录
python -m src.main --paper --replay 12

# 实时监听；需要 Telegram 配置及登录
python -m src.main --paper
```

这些命令访问网络，并可能更新 `data/state.json`、`logs/tradelife.log`。公开页文字 replay 不是新框架的冻结离线 eval，也不等同于实时原图管道。学习实验使用独立环境，不直接用这条入口生成 lab 结果。

现有真实订单路径由 `--live` 与 `LIVE_TRADING=1` 双开关控制，`--paper` 优先覆盖。缺所需凭证时 live 连接可能失败退出，并非自动回退 paper；GMGN 另有自动交易开关。当前 L0–L7 不包括启用这些开关、申请 testnet 资金或切换主网。

## 给另一个 agent / 另一台机器

传递源代码、测试、`config.yaml`、`requirements.txt`、`.env.example`、README、handoff 与唯一手册。接手先核对实际已完成批次，再从 F0 或真实进度继续。

`.gitignore` 排除了 `data/`、`logs/`、`.env` 与 session，复制代码可能没有历史素材。缺素材时先用明确标注的教学样例做 F0，不伪造历史评测结果。凭证在目标环境单独配置，不把私钥或登录态当作学习材料传递。

Lab 入口为 `python -m src.lab.cli`（见上文）。没有打包或部署；数据库默认在被 git 忽略的 `data/lab/`。
