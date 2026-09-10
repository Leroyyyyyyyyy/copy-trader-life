# Tradelife · Agent 学习与应用项目

用 Telegram 消息、图表和模拟交易任务学习 agent。**当前任务：实施 L0–L7，按 F0–F7 推进，完成后停在 L7。** 使用 coding agent 协助写代码，项目本人需要能独立解释设计、关键实现、实验和失败恢复，准备面试答辩。

## 项目入口

- **唯一详细设计**：[Agent 学习与实施手册](docs/agent_architecture_v2.md)。先读 [1.7 框架规格](docs/agent_architecture_v2.md#framework-l0-l7)与 [1.8 制作和答辩要求](docs/agent_architecture_v2.md#coding-and-defense)。
- **接手实施**：[handoff.md](handoff.md)，包含真实状态、下一批工作与可复制提示。
- README 维护入口与运行说明，handoff 维护进度，手册维护设计与验收；不复制成三份架构。

## 当前状态（2026-09-10）

| 部分 | 状态 |
|---|---|
| 原有 Telegram → parser → Engine → Hyperliquid/Solana 代码 | 已存在，是业务与测试基线 |
| L0–L7 模块、接口、实验、恢复、eval 与人工复核设计 | 已完成文档 |
| `src/lab/`、`tests/lab/`、`experiments/` | 尚未实现 |
| 当前接手起点 | F0：TaskSpec/EvalSpec、冻结样例、独立单例 verifier |
| 当前终点 | F7 / L7：人工审核、恢复和完整批量 eval |
| L8–L11、SFT/RL、推理集群与模型发布 | 后续资料，不属于本次实施 |

这次更新只修改文档，未启动应用、训练或部署，也未重跑测试。历史 testnet 和测试记录见 handoff，不代表当前环境已验证。

## 实施方式

按手册 F0–F7 分批交付可运行增量，在 `src/lab/` 建立独立模拟实验框架，保留原有业务入口。每批提供真实检查结果、关键路径讲解、trace 与本人理解练习；代码通过和本人已掌握分别记录。

初期使用 stub 模型、SQLite/文件、只读或模拟工具；L5 才加入 LangGraph 对照，L7 先用 CLI 复核。H1/H2/H3、压缩、记忆与 Skills 实验都在手册中。Lab 的命令与依赖尚未实现，手册配置不是可运行 CLI。

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

解析/风控单测不需要 Telegram 登录或交易凭证。现有全量命令为 `python -m unittest discover -s tests -v`；先检查当前测试的 mock/存储隔离，再执行并记录实测结果。本轮未重跑测试。新 lab 用例未来放在 `tests/lab/`，其发现方式随实现补入本节。

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

目前没有可部署的 lab 包或启动命令。先完成 F0–F7；实现出入口后，将真实安装/运行步骤更新到这里。
