# Tradelife 交接 · 当前执行 L0–L7

> 更新：2026-09-10。详细设计只维护在 [Agent 学习与实施手册](docs/agent_architecture_v2.md)。
> 当前明确任务：实施 L0–L7，按 F0–F7 推进，终点为 F7/L7。旧交接的“下一步实盘验证 / 切主网”不再适用。

## 1. 用户目标和协作要求

通过项目学习 agent 应用，并能在面试中独立解释与答辩。Coding agent 协助实现；每批提供可运行增量、实际检查结果、关键代码/trace 讲解与本人理解练习。分别记录实现和理解状态，不替用户认定“已掌握”。

本次交接覆盖最小 eval、共用 harness、两个模拟环境、上下文/记忆/Skills、H2 观察消融、H3 沙箱程序工具、多 agent 并发、native/LangGraph、隔离/trace、人工复核与恢复。不继续 L8–L11，不开始 SFT/RL，不部署原有实盘交易器。

## 2. 实际状态与阅读顺序

| 部分 | 当前状态 |
|---|---|
| 原有 `src/` 与 `tests/` | 已存在；可作为领域行为和回归素材 |
| 框架规格、学习验收与本人答辩要求 | 已写入手册 |
| `src/lab/`、`tests/lab/`、`experiments/` | 2026-09-10 检查时尚不存在 |
| Lab 实现/运行/部署 | 尚未进行；本轮只更新文档 |
| 测试通过数、凭证、网络与账户状态 | 本轮未核验，不以旧记录代替当前验证 |
| Git | 本次工作目录未见 `.git/`；不要假定能直接提交或有远程仓库 |

先读 [README](README.md)，再读手册 [1.7 框架规格](docs/agent_architecture_v2.md#framework-l0-l7)、[1.8 制作与答辩](docs/agent_architecture_v2.md#coding-and-defense)及第 13.1 节目录。随后只读当前批次对应验收，不需要先实现后半本训练架构。

接手先核对文件与已有修改；若别的 agent 已有成果，应保留并从真实完成位置继续，不按旧状态覆盖工作。

## 3. 第一批直接做 F0

目标是建立最小、可独立判分的任务契约，不先搭全量框架：

1. 检查仓库规则与实际文件，读 `src/models.py`、`src/parser.py` 和现有解析测试。
2. 在手册规定范围定义 TaskSpec、EvalSpec、CaseScore，将 agent 可见观察与隐藏标签分开；记录材料可见时间、任务族与版本。
3. 准备少量冻结样例：正确关联、歧义复核、错计划、已关闭计划等；缺真实素材可用明确标记的教学样例。
4. 实现不调用 LLM/venue 的独立单例 verifier。覆盖正确、错误、不完整结果，拒绝“格式正确但计划错”和自报成功。
5. 给出真实测试结果、运行方法、一个完整判分例子与局限，带本人做一个变式练习；未练习的理解状态保持待验证。

之后按 F1→F7 做可评审增量，依赖和出口条件见手册 1.7.10。已授权范围内不需要每改一个文件都重新申请确认；遇到必要缺失输入时单独说明。

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

先核对实际文件与已有修改。如果 lab 仍未实现，从 F0 的任务/隐藏标签分离、
教学样例和独立单例 verifier 开始；如果已有成果，保留并从真实进度继续。
每批给出可运行增量、实际检查结果、关键代码和 trace 讲解、本人理解练习。
代码验证与本人理解分别记录，不代替本人认定已掌握。

本次只实施模拟学习框架，不启动原 live 监听、不申请资金、不切主网，
不共享生产 state/session，不开始 SFT/RL。H3 没有合格沙箱则保持禁用。
使用 src/lab/、tests/lab/ 与手册规定的实验目录。完成每批后更新交接进度，
真实 CLI 可运行后再更新 README。不要把文档草案当作现有代码。
```

## 7. 每批完成后更新什么

handoff：记录日期、真实批次状态、修改入口、执行命令与结果、问题、下一步及本人理解状态。README：只补真实安装/运行入口与依赖。手册：更新设计决策与验收清单，避免重复存放实现日志。
