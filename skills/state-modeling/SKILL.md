---
name: state-modeling
description: 把游戏规则建模为确定性的 WorldState 操作、校验、玩家权限分层与可回放的回合结算，保持引擎场景无关。
---

# 状态建模

## 何时使用

新增状态字段、StateOp、条件种类、回合结算步骤、存档语义，或调整玩家可用操作时使用。

## 输入

规则文本、状态主体与允许的转换、拒绝条件与错误码、在回合结算中的位置、对旧存档的影响。

## 步骤

1. **先问是否该进引擎**：只有场景无关的机制进引擎（`backend/simcore/domain/`），具体数值与文案由剧本配置（settings、formulas、YAML 字段）。
2. 在 `domain/models.py` 定义状态；新字段缺省为 `None` 且不序列化，旧存档的状态哈希才不变。
3. 在 `domain/ops.py` 定义带 `op` 判别字段的操作；在 `domain/rules.py` 做纯校验（稳定错误码 + 中文说明），在 `domain/apply.py` 做确定性应用并产出可读事实（显示名、原因、前→后）。
4. **权限分层**（`domain/permissions.py`）：决定新 op 是剧本层专用还是玩家层可用；玩家层再加对象限制（本方、敌对、在场）。越权一律 `OP_NOT_PERMITTED` 并说明正确途径。
5. 若随回合变化，在 `domain/turn.py` 明确插入的步骤（现顺序：到期抉择 → 延期条款 → 常设令 → 账目与养兵 → 收入 → 工程 → 抵达 → 军令与战斗、AI 行军 → 漂移 → 事件 → 计数器 → 结局 → 历法 → 远地军报送达 → 危机计）。随机只用传入的种子 RNG；需要独立随机时派生子流，不扰动事件掷骰序列。
6. 影响对账或危机变化的步骤，要把变化归入正确的分段（诏令 / 延期 / 经常收支 / 事件），保证各段之和恰为期末 − 期初。
7. **公开视图**：新状态若含隐藏信息，在 `domain/visibility.py` 的公开视图与 `domain/digest.py` 的 StateDigest 中只给区间或省略；MCP 与 `/view` 只能读公开视图。
8. `validate_scenario` 为新的剧本字段加结构与引用检查；`invariants.py` 加不变量（测试中每次结算后运行）。

## 输出契约

类型化操作、明确的事实与拒绝原因、权限分层、可重放的状态转换、校验与测试；旧剧本与旧存档不受影响。

## 涉及文件

`backend/simcore/domain/`、`simcore/scenario/loader.py`、`simcore/store/session.py`、`backend/tests/`、`docs/SCENARIO_GUIDE.md`（字段说明）。

## 经验

- 回执、事实、简报、官职总表、StateDigest 必须用**同一个函数**给出同一件事的说法（如「遥领 / 已启程赴任」），否则模型会报告「前后矛盾」。
- 同一组条款（一个奏疏选项）要么一起执行要么一起延期——按组掷骰。
- 未确认的草案不能被结算悄悄丢掉（`DRAFT_UNCONFIRMED`）。

## 验证

成功、边界、拒绝各一测；`SIM_CHECK_INVARIANTS=1` 下全部测试通过；同一日志重放的 `state_hash` 一致（`sim replay`）。
