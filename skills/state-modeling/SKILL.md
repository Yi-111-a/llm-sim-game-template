---
name: state-modeling
description: 将游戏规则建模为确定性 WorldState 操作、验证约束与可回放事件。
---

# 状态建模

## 何时使用

新增状态字段、操作类型、资源规则、项目、移动或存档语义时使用。

## 输入

规则文本、状态主体与允许的转换、错误条件及回合顺序。

## 步骤

1. 明确操作前置条件、效果、拒绝代码和可观察事实。
2. 在 `domain/models.py` 定义状态，在 `domain/ops.py` 定义带 `op` 判别字段的操作。
3. 在 `domain/rules.py` 做纯验证，在 `domain/apply.py` 做确定性应用；不在此处调用模型或随机网络服务。
4. 若状态会随回合改变，在 `domain/turn.py` 明确插入顺序并保证种子 RNG 可重放。
5. 确认 JSONL 日志足以重建状态，必要时更新快照和哈希测试。

## 输出契约

类型化操作、明确的成功事实与拒绝原因、可重放状态转换及测试。

## 涉及文件

`backend/simcore/domain/`、`backend/simcore/store/session.py`、`backend/tests/`。

## 验证

为成功、边界和拒绝各写测试；检查同一事件日志重放后的 `state_hash` 一致。
