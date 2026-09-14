---
name: verify_exit_protect
description: Check a stop-to-cost update against the filled plan's known cost basis.
---

# 核验退出保护更新

1. 先读账户快照，确认目标计划 status 为 filled，并记下 cost_basis 与当前 stop_loss。
2. 跟帖说「移到成本」时，提议的 stop_loss 必须等于该计划的 cost_basis。
3. 只改目标计划；提交前核 expected_state_version。
4. 计划已关闭或找不到唯一目标时 ignore 或 review，不要改其它计划。
5. 本技能只描述核验步骤，不扩大工具权限，也不包含任何任务的标准答案。
