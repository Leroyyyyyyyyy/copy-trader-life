# Tradelife 交接 · 当前执行 L0–L7

> 更新：2026-09-19（L5 native/LangGraph 对照已落地；F5 收口）。详细设计只维护在 [Agent 学习与实施手册](docs/agent_architecture_v2.md)。
> 当前明确任务：实施 L0–L7，按 F0–F7 推进；下一批为 F6（L6 隔离/故障/trace）。终点为 F7/L7。旧交接的“下一步实盘验证 / 切主网”不再适用。

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
| L4 多角色编排 / 证据汇合 | 已实现；`tests.lab.test_l4_roles` 9 passed |
| L5 native/LangGraph WorkflowAdapter | 已实现；`tests.lab.test_l5_workflows` 9 passed |
| 凭证、网络与账户状态 | 本轮未核验 live；lab 不依赖 |
| Git | 仓库存在；提交与否由本人决定 |

先读 [README](README.md)，再读手册 [1.7 框架规格](docs/agent_architecture_v2.md#framework-l0-l7)、[1.8 制作与答辩](docs/agent_architecture_v2.md#coding-and-defense)及第 13.1 节目录。随后只读当前批次对应验收，不需要先实现后半本训练架构。

接手先核对文件与已有修改；若别的 agent 已有成果，应保留并从真实完成位置继续，不按旧状态覆盖工作。

## 3. 下一批做 F6（F0–F5 已完成）

L5 已交付：`make_workflow_adapter("native"|"langgraph")` 共用 `src/lab/workflows/nodes.py` 的 classify → resolve → fan_out → risk → merge → commit。只换编排表达：native 是 SQLite FSM checkpoint，LangGraph 是 `StateGraph` + `InMemorySaver` + `interrupt_before`。对照三任务同一 verifier；提交后/checkpoint 前崩溃靠 `SimulationGateway` receipt 防重复副作用。LangGraph checkpoint 进程内有效，native 可换 adapter 实例恢复。本批不做 L6 故障集与人工复核。实测：`python -m unittest tests.lab.test_l5_workflows -v`（73 lab tests，3 项因无 Docker 跳过）。

F6 目标见手册 1.7.8 之前的 L6 / 7：注入/权限/资源/取消故障集和结构化 trace；每个故障有确定状态、原始证据及资源清理记录。

评测框架仍是 **DeepEval + 自定义确定性 verifier**。DeepEval 适配器等轨迹稳定后再接。

按 F6→F7 继续可评审增量。已授权范围内不需要每改一个文件都重新申请确认；遇到必要缺失输入时单独说明。

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

先核对实际文件与已有修改。F0–F5 已落地则从 F6 继续；若更早批次缺失，
从真实缺口补起。保留已有成果，不要覆盖。
每批给出可运行增量、实际检查结果、关键代码和 trace 讲解、本人理解练习。
代码验证与本人理解分别记录，不代替本人认定已掌握。

本次只实施模拟学习框架，不启动原 live 监听、不申请资金、不切主网，
不共享生产 state/session，不开始 SFT/RL。H3 没有合格沙箱则保持禁用。
使用 src/lab/、tests/lab/ 与手册规定的实验目录。完成每批后更新交接进度，
真实 CLI 可运行后再更新 README。不要把文档草案当作现有代码。
```

## 7. F0 / L0 Eval 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：给定 agent 的结构化决定、提交后的最终账户快照和隐藏标签，独立判断任务是否真的完成；不能把“输出格式正确”或 agent 自报成功当成正确，也不能让 agent 看到 gold label。

关键路径：agent 侧 `load_agent_view` 只加载 `TaskSpec + ObservationBundle` → agent 产生 `SubmittedDecision` 和最终快照；评测侧再由 `load_for_eval` 单独加载 `EvalSpec` → `verify_plan_decision` 比较决定类型、目标计划、真实状态变化、证据数和提交次数 → 输出 `CaseScore`。标签只存在于 `experiments/fixtures/labels/`，观察与任务若混入标签键会由 `LabelLeakError` 拒绝。

设计取舍：F0 使用确定性 hard gate，不用 LLM judge，也不访问 venue。协议错误（缺证据、缺提交、重复提交）与语义错误（错计划、错止损、只口头声称成功）分开记录；`acceptable_outcomes` 允许 review/ignore 等多个合法结果，而不是强迫所有样例只有一个字符串答案。替代方案 DeepEval 只适合后续补充开放文本质量，不能替代最终状态校验。

失败案例：更新了 `plan_btc` 即使 JSON 完整也会触发 `wrong_plan` 或 `non_target_plan_changed`；声称成功但 `submit_count=0`、状态未改变会失败；正确更新但没有最少证据属于 protocol fail；歧义样例擅自猜一个 ETH 计划失败，提交 review 且保持状态不变才是可接受结果。

实验依据：`tests.lab.test_f0_verifier` 13 项通过，覆盖标签隔离、正确更新、错计划、口头成功、缺证据、歧义 review、closed ignore 和三份手写结果。当前只有少量教学 fixture、确定性规则和离线状态；没有真实模型泛化结论，也没有证明业务收益。

本人练习：已完成一个状态未落地的小变式。题设为 decision 正确指向 `plan_a`、证据和提交次数满足协议，但最终快照的止损仍为 1650；本人判断为 semantic fail，理由是实际结果没有修改。随后追问并理解了隐藏 `EvalSpec` 是评测器专用的答案、约束与可信初始基线，不进入 agent 可见路径。

掌握状态：需巩固。已能区分“声明修改”和“最终状态改变”，后续需要在 F1 trace 中继续巩固 protocol/semantic 边界及三类评测输入。

## 8. F1 / L1 Harness 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：让消息分析和计划模拟两个环境复用同一个 agent loop，并保证工具错误可回到下一轮、预算可以终止失控循环、模拟提交具备版本校验和幂等性。

关键路径：`AgentHarness.run` 建立 run/event trace → 每轮先记模型预算 → `environment.build_prompt` → `ContextBuilder.build` → `model.generate` → `ToolDispatcher.execute` → 工具结果追加为 observation 并进入下一轮；出现 final、预算耗尽或不可恢复异常后生成 `AgentResult`。变更由环境工具进入 `SimulationGateway.commit`，评测在 loop 外读取 decision 与 final snapshot。

设计取舍：loop 只依赖环境的 prompt、tool registry、last decision 和 final snapshot，不把两个环境的业务分支硬编码进 harness。未知工具和解析错误默认转成可恢复 observation；每轮最多接受一次成功 mutation。相同 run/idempotency key 与相同 payload 重试复用 receipt，不重复改变状态；同 key 不同 payload 则拒绝，而不是猜测调用方意图。

失败案例：调用不存在的工具返回 `unknown_tool` 后仍可在下一轮恢复；超过 `max_model_calls` 结束为 `TIMED_OUT`；同一幂等键换 payload 返回 `idempotency_payload_mismatch`；只有 final 文本、没有提交时，harness 保留 claimed-done 事实，最终由 verifier 判失败。

实验依据：`tests.lab.test_f1_harness` 6 项通过，覆盖两个环境共用 loop、未知工具恢复、模型预算终止、重复提交复用 receipt 和 payload 冲突。测试使用脚本化 stub 与 SQLite 临时库；尚未验证真实模型的解析错误分布、延迟或长任务稳定性。

本人练习：已完成模型调用预算变式。题设为 `max_model_calls=2` 且准备进入第三次调用；本人最初判断应进入人工复核。对照当前实现后确认 F1 尚未接入 L7 人工审核队列，`BudgetTracker.record_model` 会抛出 `BudgetExceeded`，harness 记录 `budget_exceeded` 并以 `TIMED_OUT` 终止。随后能正确判断：歧义任务主动提交且被 verifier 接受的业务 `REVIEW`，run 状态仍是 `SUCCEEDED`。该练习区分了运行终止状态与业务决策。

掌握状态：需巩固。已能区分 `TIMED_OUT` 与成功的业务 `REVIEW`；后续仍需沿完整事件 trace 解释 `FAILED` 和工具错误恢复。

## 9. L2 Context / 压缩本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：长 run 中缩短下一轮模型看到的 active context，同时保留可审计的原始事件，避免压缩把 tool call 与对应结果拆开，或把错误摘要伪装成历史事实。

关键路径：`AgentHarness` 累积不可改写的 events → `group_history` 把一个 model turn 与其全部 tool results 组成一组 → `ContextBuilder.build` 固定保留 task/world/tools/state/memory/skills → 超阈值时 `_compact` 仅摘要较老组并保留最近组 → request manifest 记录纳入的 event seq 和 compaction record。`RunStore` 中的原始 trace 不因摘要改变。

设计取舍：任务、当前世界状态、工具 schema 等 pinned 内容不参与摘要；action/observation 组是最小裁剪单位。当前采用确定性抽取摘要，保留 plan id、价格、工具、错误和 source event id。若 pinned 内容加最近组本身已超预算，则明确失败，不通过继续删关键上下文来假装可运行。摘要只是 working memory，不是 gold label 或原始证据。

失败案例：summarizer 抛错时使用带 source event ids 的声明式 fallback；注入错误价格 `9999` 只污染摘要，不改写原始 tool result；无法压缩的前缀触发 `context_budget_error`。最后一轮 token 变少不代表整个 run 一定更便宜，因为摘要本身也有成本。

实验依据：`tests.lab.test_l2_context` 6 项通过，覆盖调用/结果不拆分、触发压缩并保留证据、full 与 compact 最后一轮比较、错误摘要隔离、摘要失败 fallback 和不可压缩时显式失败。当前是字符数 token 估算与确定性 summarizer，尚未用真实 tokenizer/模型测总成本和任务质量。

本人练习：已完成三个压缩变式。其一，原始 tool result 记录止损 1800、摘要错误写成 9999；本人正确判断原始 trace 不应被改写，审计以原始 tool result 为最终证据。其二，pinned context 加最近 action/result 组本身超过阈值时，本人判断应报错停止，因为这些关键内容不能继续删除。其三，面对“最后一轮变短不等于总成本下降”，本人首先指出压缩可能丢失关键信息；经提示后选择按整个 run 的累计 token、摘要和重试成本比较，而不是只看最后一轮 active tokens。

掌握状态：已通过本次理解检查。已能解释摘要与原始证据隔离、不可删除边界，以及单轮输入缩短不等于总成本下降；后续实现真实 tokenizer/summarizer 时需重新验证成本结论。

## 10. L3 Memory / Skills 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：把跨轮长期事实与当前 run 的候选记忆分开，并让模型只看到技能目录、按需加载技能正文；避免临时候选污染后续实验、过期事实继续生效、技能正文无条件占满上下文。

关键路径：长期记忆由 `MemoryStore.publish/propose/supersede` 写入 published SQLite 或 run overlay → `retrieve` 按 snapshot、状态、时间和 scope 过滤 → `AgentHarness._memory_prompt_bits` 放进 prompt。技能由 `SkillRegistry.load_directory` 校验 `SKILL.md` frontmatter 与根目录边界 → `catalog` 只暴露名称/描述/版本 → `invoke_skill` 返回正文并记录本 run 已加载名称 → 下一轮 prompt 的 `loaded_skills` 才包含正文。

设计取舍：`propose` 只进入当前 store 的 overlay，复制 snapshot 时只复制 active published rows；更正采用旧记录 superseded、新记录 active，并保留 `supersedes` 链。冲突记忆一起返回，不静默投票。skill 是提示性流程而不是新工具权限，调用 skill 不改变环境 tool set；正文 hash 形成版本。`none/catalog/preload` 三种模式用于比较不加载、按需加载和预加载。

失败案例：expired/superseded 记忆在对应时间不可见；包含隐藏标签关键词的记忆被拒绝；两个来源冲突时都返回给上层处理；未知 skill 返回可恢复 `unknown_skill`；重复名称、越过 skill root 和缺 frontmatter 会在注册时失败；新 run 不继承上一个 run 已加载的正文。

实验依据：`tests.lab.test_l3_memory_skills` 13 项通过，覆盖记忆发布/候选/过期/替代/冲突/snapshot 隔离、标签防漏、技能目录校验及 none/catalog/preload 行为。当前检索只是受控 SQLite 过滤和子串查询，标签防漏是关键词护栏；尚未运行真实模型的三组质量—token 对照，也未证明长期记忆一定提升任务表现。

本人练习：已完成候选隔离与冲突处理两个变式。其一，当前 run 通过 `propose_memory` 产生候选 overlay 后复制新 snapshot；本人正确判断新实验不应自动看到该候选，因为它尚未发布为长期记忆。需注意候选在当前 store/run 的 overlay 中仍可检索，只是 `copy_snapshot` 仅复制 active published rows。其二，面对 chart=1650 与 text=1800 两条冲突记忆，本人判断 store 应把两条都返回上层，因为裁决证据冲突不属于 MemoryStore 的职责。其三，在 20 个 skills 仅需一个的场景中，本人正确选择 `catalog`，避免 `none` 无法使用技能，也避免 `preload` 把全部正文放入初始上下文。其四，面对 `m2 → m3` 更正，能正确说明旧记录应标记 `SUPERSEDED`、由 `m3.supersedes = m2` 保留替代链，正常检索只返回 active 的 `m3`。

掌握状态：已通过本次理解检查。已能解释候选与发布隔离、冲突不在 store 层静默裁决、记忆替代链，以及 Skills 的 none/catalog/preload 取舍。

## 11. H2 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：同样的计划模拟任务、工具和账户状态，只改变是否把「当前允许动作」写进观察，比较它对两种固定决策策略的影响。

关键路径：`run_h2_matrix` → `PlanSimulationEnvironment.build_prompt` → `allowed_actions_from_plans(gateway.plans())` → `ContextBuilder` → stub `generate` → `submit_plan_update` / `submit_decision` → `verify_plan_decision`。

设计取舍：列表按 filled/not-filled 生成全部计划的合法性，不按 gold 只保留「正确」那张。因此模糊双 ETH 两条更新都合法，列表帮不了会猜的模型。

失败案例：改已关闭计划会被环境拒绝（`plan_not_filled`），与缺字段的 schema 错误分开计数。

实验依据：stub A0 完成 1/3、A1 2/3；B0/B1 均为 3/3；A 的环境拒绝从 1 降到 0，输入 token 上升。真实模型未跑。

本人练习：已完成两个 H2 判断。其一，两个 ETH 计划都为 `filled`、naive A 随机选择其一且环境允许提交，但标签要求歧义时 `REVIEW`；本人正确判断这是 semantic fail，因为动作约束层只能判断是否合法，不能判别哪个合法动作语义正确。其二，面对 A0=1/3、A1=2/3，能指出样本过小，不能直接宣称真实 LLM 准确率提升；经提示后选择正确的受限结论：它只在当前受控 stub 实验中减少了一类非法动作，同时增加输入 token，真实模型效果仍待测。

掌握状态：已通过本次理解检查。已能解释 allowed-actions 约束合法性但不提供语义答案、2×2 结果的局部含义，以及小样本/stub/token 成本对结论范围的限制。

## 12. H3 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：模型生成的 Python 如何批量模拟候选动作，又保证共享教学账户只被提交一次，且不在宿主进程执行不可信代码。

关键路径：`run_program` → `DockerSandbox.run_program`（`docker run --network none`）→ 容器内 `guest.py` 调用 `simulate_action` 分叉快照 → 返回 `tool_trace`/`proposed_action` → 宿主 `commit_simulated_action` → `SimulationGateway.commit`。

设计取舍：模拟逻辑是纯函数，容器不回调宿主、不改 SQLite。Docker 不可用时禁用 `run_program`，不退回 `exec`。

失败案例：guest 抛错记为可恢复 `program_error`；超时/daemon 缺失是基础设施错误；旧 `expected_state_version` 提交被拒绝且不二次生效。

实验依据：多次模拟只提交一次、异常后改走 `submit_plan_update` 恢复、版本冲突拒绝；展开调用 ≥2。真实模型未跑。

本人练习：已完成两个 H3 变式。其一，沙箱程序连续调用 `simulate_action` 三次比较候选；本人正确判断这些调用不应修改共享账户，因为它们只在分叉快照上产生候选，最终共享账户最多由宿主 commit 发生一次状态变更。其二，候选基于 version 3、提交前共享状态已到 version 4 时，本人正确判断旧候选不能直接提交，并提出重新开分支；需明确旧提交先以 stale version 拒绝，再读取 version 4 并基于最新快照重新模拟，而不是沿用旧候选。其三，能判断程序内 3 次 `simulate_action` 都要作为展开调用计入预算；最初理由表述为“消耗 token”，经说明后区分为外层 1 次工具调用加 3 次程序展开调用，展开调用即使不调用模型也消耗计算并必须防止预算绕过。最后能正确选择 Docker 不可用时禁用 `run_program`、改用安全结构化工具，而不是回退宿主 `exec`。

掌握状态：已通过本次理解检查。已能解释分叉模拟与单次宿主提交、stale version 重算、展开预算及“无沙箱则禁用”的安全边界。

## 13. L4 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：跟帖改止损要拆成分类、消歧、只读读图/账户、风控和汇合；串行与并发必须同规则提交一次，图文冲突不能靠投票消掉。

关键路径：`classify` → `resolve_context`（等标的）→ `read_chart` + `read_market` fan-out → `review_risk` → `merge_evidence` → 宿主一次 `submit_plan_update` / `submit_decision`。

设计取舍：七个角色不都调 LLM。Chart 读冻结 `chart_notes`，不跑视觉。Context 与 Chart 不同时启动——图依赖 resolver 的 symbol。`single` 跳过 fan-out，用来对照「多角色不一定更好」。共享预算在 fan-out 前预留，避免两分支各花到上限后再提交。

失败案例：chart 注入的 `plan_id` 与 resolver 不一致 → review，ETH 止损不变；market 失败标记 `market_context` 缺失仍可提交；`max_tool_calls=2` 预留后挡住 commit；慢 chart/market 不产生第二次提交。

实验依据：`tl_f0_move_sl_plan_a` / `closed_plan` / `ambiguous_eth` 在 `single`/`serial`/`concurrent` 下 hard_pass；唯一更新只改 plan_a，BTC 止损不动。真实模型未跑。

本人练习：开始检查并发边界。面对“chart/market 可并发而 resolve/chart 不可直接并发”，本人首先回答存在先后顺序；经说明后正确确认并发前提是输入已准备、彼此无数据依赖且不会并发修改共享状态。Chart 需要 ContextResolver 先确定 symbol/候选计划，而 Chart 与 Market 获得同一已解析上下文后互不依赖，才适合只读 fan-out。

掌握状态：需巩固。已能确认数据依赖和只读边界，并在 Resolver=`plan_a`、Chart=`plan_b` 的冲突场景中正确选择记录冲突、转 `REVIEW` 且不修改账户，而不是用投票掩盖证据分歧；还需检查共享预算和单次提交。

## 14. L5 本人理解与答辩（代码已验证 / 本人理解待练习）

解决的问题：同一条「分析 → 补证据 → 复核 → 提交」流程，用原生 FSM 和 LangGraph 两种编排表达对照；证明换框架不等于换模型或角色，且 checkpoint 不能单独保证提交只生效一次。

关键路径：`make_workflow_adapter` → 共用 `execute_node`（classify/resolve/fan_out/risk/merge/commit）→ native 把 `next_node` 与节点结果写入 `RunStore.checkpoints`，LangGraph 用 `StateGraph.compile(checkpointer=InMemorySaver, interrupt_before=...)` → 模拟账户仍只经 `SimulationGateway.commit`。恢复时按 last checkpoint 的 next step 续跑；若 commit 已成功但 checkpoint 未更新，重跑节点会命中同键 receipt 并 `reused=True`。

设计取舍：L5 只换编排，不改角色拓扑。这条线性教学路径 native FSM 更短、SQLite checkpoint 可跨 adapter 实例恢复；LangGraph 省去手写 interrupt/resume 循环，但 `InMemorySaver` 不能换进程，且节点失败后默认重跑。副作用幂等放在 gateway，不放在图里。

失败案例：`crash_after_commit` 时账户已改、checkpoint 仍停在 `commit`；resume 后 `submit_count` 仍为 1，BTC 止损不变。`crash_before_commit` 时尚未提交，resume 才提交一次。`interrupt_before=("commit",)` 暂停时 `submit_count=0`。两套 backend 使用独立 gateway/快照，不能共用一份账户。

实验依据：`tests.lab.test_l5_workflows` 9 项通过，覆盖三任务对照、账户隔离、interrupt、提交前后崩溃、fan-out 单次提交。真实模型与跨进程 LangGraph 持久化未测。固定 API：langgraph 1.2.x 的 `StateGraph`、`compile(..., interrupt_before=)`、`InMemorySaver`、`invoke(None, config)` 续跑、`get_state(config).next`。

本人笔记（2026-09-20，checkpoint 两件事）：

1. **LangGraph checkpoint 不跨进程 / 不跨 adapter 实例。**  
   `make_workflow_adapter("langgraph")` 把 `store` 丢掉，只构造 `LangGraphWorkflowAdapter(runtime)`；图状态进进程内 `InMemorySaver`，键是 `thread_id=run_id`。新 adapter = 新 saver = 空图，看起来像没跑过。所以 `test_langgraph_interrupt_before_commit` / `test_langgraph_crash_after_commit_reuses_receipt` 必须对**同一个** `adapter` 再 `run_or_resume`。  
   Native 相反：checkpoint 写 `RunStore.checkpoints`。`test_native_interrupt_then_new_adapter_resumes` 和 crash 恢复都是 `make_workflow_adapter` 再拿一次，同一 `store` + `run_id` 就能续。这是存储位置差异，不是「LangGraph 不能持久化」——本轮故意没用 SQLite checkpointer。

2. **checkpoint 不保存账户；失败节点会重跑；exactly-once 在 gateway。**  
   Native / LangGraph 的 checkpoint 都只存运行引用、节点结果、`next_node`，没有 `plans` / `submit_count`（见 `test_native_checkpoint_omits_account_snapshot`）。账户只在 `SimulationGateway`。  
   `crash_after_commit` 时：`commit_merged` 已经 `handle_tool` 改了账户并写入 receipt，随即 `InjectedCrash`。节点没成功返回，所以 **checkpoint 仍停在 `commit`**（native 的 `_save` 在 `execute_node` 之后，进不去）。Resume 会再进 `commit`。  
   不重复改账户是因为幂等键 `run_id:decision:1`：`SimulationGateway.commit` 发现已有同 payload receipt 就 `reused=True`，不再 `submit_count += 1`，也不再改 `plan_btc`。图只负责「从哪续」；「副作用只生效一次」不由图保证。

3. **把节点拆细只能缩小重跑范围，不能消除副作用窗口。**  
   即使把提交拆成「检查 → 修改账户 → 记录成功 → checkpoint 前进」，仍可能在修改账户后、记录成功前崩溃；并发执行器也可能同时通过“尚未提交”的检查。因此只读的 prepare/validate 节点可以放心重跑，但真正的 commit 节点仍必须使用稳定幂等键。只有账户修改、receipt 与进度更新能处于同一原子事务时，才可能由事务一起保证；真实外部交易所通常无法加入本地 SQL 事务。

4. **SQL saver 是 checkpoint 的持久化实现，不是 checkpoint 的替代品。**  
   把 LangGraph 的 `InMemorySaver` 换成 SQLite/Postgres saver，可以跨进程找回 graph state 和下一节点；当前 native 已经把 checkpoint 放在 SQLite。SQL saver 解决“重启后找得到进度”，但若提交成功后、SQL checkpoint 更新前崩溃，恢复时仍会再次进入 commit。因此推荐组合是：SQL saver 保存 workflow 进度，`SimulationGateway` 的 SQL receipt 保证副作用幂等；两者可在同一数据库中，但职责和表应分开。

本人练习：已能解释 `crash_after_commit` 后为什么节点重跑、为什么拆细节点仍不能代替幂等，以及 SQL saver 只增强持久恢复、不负责交易状态与 exactly-once。后续可不看答案预测 resume 后的 `submit_count` 与目标/非目标计划状态，再对照 `test_native_crash_after_commit_reuses_receipt`。

掌握状态：需巩固。checkpoint、SQL saver 与幂等 receipt 的职责边界已理解；后续需要独立完成一次崩溃恢复变式。

## 15. 每批完成后更新什么

handoff：记录日期、真实批次状态、修改入口、执行命令与结果、问题、下一步及本人理解状态。README：只补真实安装/运行入口与依赖。手册：更新设计决策与验收清单，避免重复存放实现日志。
