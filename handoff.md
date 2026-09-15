# Tradelife 交接 · 当前执行 L0–L7

> 更新：2026-09-15（H3 Docker 沙箱已落地；F3 收口）。详细设计只维护在 [Agent 学习与实施手册](docs/agent_architecture_v2.md)。
> 当前明确任务：实施 L0–L7，按 F0–F7 推进；下一批为 F4（L4 多 agent）。终点为 F7/L7。旧交接的“下一步实盘验证 / 切主网”不再适用。

## 1. 用户目标和协作要求

通过项目学习 agent 应用，并能在面试中独立解释与答辩。Coding agent 协助实现；每批提供可运行增量、实际检查结果、关键代码/trace 讲解与本人理解练习。分别记录实现和理解状态，不替用户认定“已掌握”。

本次交接覆盖最小 eval、共用 harness、两个模拟环境、上下文/记忆/Skills、H2 观察消融、H3 沙箱程序工具、多 agent 并发、native/LangGraph、隔离/trace、人工复核与恢复。不继续 L8–L11，不开始 SFT/RL，不部署原有实盘交易器。

## 2. 实际状态与阅读顺序

| 部分 | 当前状态 |
|---|---|
| 原有 `src/` 与 `tests/` | 已存在；可作为领域行为和回归素材 |
| 框架规格、学习验收与本人答辩要求 | 已写入手册 |
| F0 `src/lab/domain.py` + `src/lab/evals/` + fixtures | 已实现；`tests.lab.test_f0_verifier` 13 passed |
| F1 harness / runtime / stub / 两环境 / SimulationGateway | 已实现；`tests.lab.test_f1_harness` 覆盖成功/恢复/预算/幂等 |
| L2 ContextBuilder / 压缩 | 已实现；`tests.lab.test_l2_context` |
| L3 SQLite memory / SkillRegistry | 已实现；`tests.lab.test_l3_memory_skills` |
| H2 观察消融（stub 2×2） | 已实现；`tests.lab.test_h2_observation` |
| H3 Docker SandboxExecutor | 已实现；`tests.lab.test_h3_sandbox` |
| 凭证、网络与账户状态 | 本轮未核验 live；lab 不依赖 |
| Git | 仓库存在；提交与否由本人决定 |

先读 [README](README.md)，再读手册 [1.7 框架规格](docs/agent_architecture_v2.md#framework-l0-l7)、[1.8 制作与答辩](docs/agent_architecture_v2.md#coding-and-defense)及第 13.1 节目录。随后只读当前批次对应验收，不需要先实现后半本训练架构。

接手先核对文件与已有修改；若别的 agent 已有成果，应保留并从真实完成位置继续，不按旧状态覆盖工作。

## 3. 下一批做 F4（F0–F3 已完成）

H3 已交付：`DockerSandbox` 在独立容器中执行模型 Python（`--network none`、只读根文件系统、内存/pids 限制）；宿主 `docker.py` 不含 `exec/eval`。`simulate_action` 只分叉快照；`run_program` 无提交接口；`commit_simulated_action` 由宿主核版本后最多应用一次。程序内每次模拟计入 `program_tool_calls`。无 daemon 时不注册 `run_program`。实测：`python -m unittest tests.lab.test_h3_sandbox -v`（55 lab tests 全过）。

F4 目标见手册 1.7.10 / 5：角色边界、共享预算、只读 fan-out、证据汇合；串行/并发同规则评分。

评测框架仍是 **DeepEval + 自定义确定性 verifier**。DeepEval 适配器等轨迹稳定后再接。

按 F2→F7 继续可评审增量。已授权范围内不需要每改一个文件都重新申请确认；遇到必要缺失输入时单独说明。

## 4. 实施边界与代码线索

- 本阶段目录：`src/lab/`、`tests/lab/`、`experiments/`；Eval 为 `src/lab/evals/`。不再建另一份架构或平行的 `src/evals/`。
- 不把现有 Engine 直接复制成多个共享环境。Lab 不调用 `Engine.start()`、venue connect，不写生产 `data/state.json`。
- 每个 run 独立状态，模拟提交统一做版本校验与幂等事务；角色只写自己的结果，不并发改同一 Signal。
- 原始 trace、摘要、长期记忆和隐藏标签分开。候选记忆不自动污染后续实验。
- H3 程序必须由独立 SandboxExecutor 执行；无合格沙箱时禁用，不回退到宿主 `exec/eval`。
- 不因实现学习框架而输出凭证、复制 Telegram session、修改实盘开关或申请资金。
- 手册中的类、函数、配置是草案，实现后再登记真实 API 和命令。

关键入口：`src/main.py:resolve_dry_run` 控制 paper/live；`src/bot.py:_handle` 卸载同步 Engine 到线程；`src/engine.py` 管理共享 context/plans、风控与执行；`src/store.py` 与 `src/util.py:dump_json` 使用 JSON 文件。现有线程卸载和原子文件替换不能替代多 run 事务隔离。

已有测试包括 parser、risk、main、chart_reader、engine_chart、live_execution、reconciliation。先检查 mock 与存储隔离再运行；本轮未执行测试。基线安装与命令见 README。

## 5. 历史记录：用于追溯，不是当前待办

以下是旧 handoff 的记录，本轮未重跑或独立审计其历史结果：

| 历史项 | 原记录 | 当前限制 |
|---|---|---|
| 图表结构单回测 | `data/charts/levels.json`、`data/popi_2026_chart_bt.json`；旧记录为 7 笔结构样本、6 成交、合计 −$9.29，#5718 跳过 | 待审计素材，不直接当 gold label 或可靠收益标签 |
| 较早文字回测 | `data/popi_2026_structure_bt.json`、`data/popi_2026_her_sl_bt.json` | 假设与按图版本不同，不混用统计 |
| 图像实时接入 | bot 下载图 → engine → `chart_reader.read_levels` | 代码存在；公开文字 replay 不等价 |
| 测试历史 | 旧记录 40 passed，补对账测试后为 44 passed | 不是当前实测通过数 |
| HL testnet | 旧记录 2026-09-03：约 $15 ETH 开仓、TP/SL 读回、撤单和平仓，最终仓位/挂单为空 | 仅当时 testnet 结果，不说明当前账户；未记录主网验证 |
| 退出单与对账 | 程序记录自己的 OID，缺退出保护标 `at_risk`，外部仓位只记录不管理其退出单 | 查 engine 与 reconciliation 测试，本轮未连接交易所 |

需要保留的数据局限：

- 图上价格、文字、事后持仓卡和行情代理不是同一证据。#7217 的卡片成本与按收线触发的模拟成交价不同，不能为贴近结果强行替换。
- `src/bt_chart_levels.py` 有 `6h → 4h` 替代，精细行情覆盖不同，同 K 线 TP/SL 先后使用假设；都应记录进环境版本。
- 旧图像管道失败后可能回退代理触发位；新框架若采用缺证据复核，应明确是行为变体。
- `data/popigogo_posts.json`、`data/channel_posts.json`、`data/popi_2026_setups.json` 是候选素材，换机器未必存在。

旧 faucet 步骤、打印新私钥的命令、主网切换操作与过时凭证表已从活动交接移除。未来如有独立交易验证任务，再核对当时状态与范围。

## 6. 可直接复制的实施提示

```text
请接手并实施 Tradelife 的 L0–L7 学习框架，按 F0–F7 分批推进，完成后停在 L7。
先读 README.md、handoff.md，再读 docs/agent_architecture_v2.md 的
1.7 框架、1.8 制作与答辩要求、13.1 目录，以及当前批次验收。
架构手册是唯一设计来源，不再新建第二份架构文档。

先核对实际文件与已有修改。F0–F3 已落地则从 F4 继续；若更早批次缺失，
从真实缺口补起。保留已有成果，不要覆盖。
每批给出可运行增量、实际检查结果、关键代码和 trace 讲解、本人理解练习。
代码验证与本人理解分别记录，不代替本人认定已掌握。

本次只实施模拟学习框架，不启动原 live 监听、不申请资金、不切主网，
不共享生产 state/session，不开始 SFT/RL。H3 没有合格沙箱则保持禁用。
使用 src/lab/、tests/lab/ 与手册规定的实验目录。完成每批后更新交接进度，
真实 CLI 可运行后再更新 README。不要把文档草案当作现有代码。
```

## 7. H2 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：同样的计划模拟任务、工具和账户状态，只改变是否把「当前允许动作」写进观察，比较它对两种固定决策策略的影响。

关键路径：`run_h2_matrix` → `PlanSimulationEnvironment.build_prompt` → `allowed_actions_from_plans(gateway.plans())` → `ContextBuilder` → stub `generate` → `submit_plan_update` / `submit_decision` → `verify_plan_decision`。

设计取舍：列表按 filled/not-filled 生成全部计划的合法性，不按 gold 只保留「正确」那张。因此模糊双 ETH 两条更新都合法，列表帮不了会猜的模型。

失败案例：改已关闭计划会被环境拒绝（`plan_not_filled`），与缺字段的 schema 错误分开计数。

实验依据：stub A0 完成 1/3、A1 2/3；B0/B1 均为 3/3；A 的环境拒绝从 1 降到 0，输入 token 上升。真实模型未跑。

本人练习：待做。建议先预测 A0 closed / A1 ambiguous 的结果，再对照 `python -m src.lab.experiments.h2_observation`。

掌握状态：待练习。

## 8. H3 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：模型生成的 Python 如何批量模拟候选动作，又保证共享教学账户只被提交一次，且不在宿主进程执行不可信代码。

关键路径：`run_program` → `DockerSandbox.run_program`（`docker run --network none`）→ 容器内 `guest.py` 调用 `simulate_action` 分叉快照 → 返回 `tool_trace`/`proposed_action` → 宿主 `commit_simulated_action` → `SimulationGateway.commit`。

设计取舍：模拟逻辑是纯函数，容器不回调宿主、不改 SQLite。Docker 不可用时禁用 `run_program`，不退回 `exec`。

失败案例：guest 抛错记为可恢复 `program_error`；超时/daemon 缺失是基础设施错误；旧 `expected_state_version` 提交被拒绝且不二次生效。

实验依据：多次模拟只提交一次、异常后改走 `submit_plan_update` 恢复、版本冲突拒绝；展开调用 ≥2。真实模型未跑。

本人练习：待做。建议指出沙箱、展开计数、单次提交三条代码边界。

掌握状态：待练习。

## 9. 每批完成后更新什么

handoff：记录日期、真实批次状态、修改入口、执行命令与结果、问题、下一步及本人理解状态。README：只补真实安装/运行入口与依赖。手册：更新设计决策与验收清单，避免重复存放实现日志。
