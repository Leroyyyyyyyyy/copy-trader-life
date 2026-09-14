---
name: link_followup
description: Link a follow-up message to a candidate plan using visible text and plan fields only.
---

# 关联跟帖

1. 列出当前可见消息，标出跟帖（通常是最新一条）。
2. 读每张计划的 symbol、side、status、note，看哪张和跟帖同标的、同方向。
3. 只有一张匹配且仍是 filled，才提交该 plan_id。
4. 两张同类计划都匹配，或跟帖没有点名计划时，提交 review，不要猜。
5. 不要把止损数字写进分析环境；那是计划模拟环境的事。
