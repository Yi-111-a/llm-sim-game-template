---
name: event-design
description: 设计条件可验证、概率可控、可重放的事件与事件抉择，并让它们与奏疏正确衔接（奏疏先于它所决定的事件）。
---

# 事件设计

## 何时使用

新增历史事件、随机遭遇、条件触发、事件抉择、远地军报或事件链时使用。

## 输入

事件的触发时点与前置条件、概率（gravity）、效果 op、抉择选项与默认项、相关奏疏。

## 步骤

1. 稳定 id、标题与 `kind`：`scheduled` / `conditional` / `random`（需插图资产），记账事件用 `kind: system` 或 `internal: true`（不展示）。
2. `when: {turn: N}` 与条件 `{kind: turn, value: N}` 同义，都指推进到第 N 回合时的那次结算。前置条件只用条件 DSL，不用自由文本。
3. 效果写成合法 StateOp，`reason` 写人话（玩家会看到「国库 −20（北境冬衣）」）。效果按「当时的局势」作用：要扣叛军兵力，就用条件选出当下属叛方的军队，不要按开局 id 盲扣。
4. **抉择**：`choices`（选项可带 `conditions` 与 `memory`）、`default_choice`、`choice_deadline`。默认项要是「不处置时自然发生的事」。
5. **与奏疏衔接**：奏疏问「是否 X」，执行 X 的事件必须读 `memorial:<id>`、在奏疏之后结算；不要写固定回合的事件先把 X 做了。「到任才生效」之类的延迟效果也写成事件（读 `character_region` + `army_commander`），而不是下诏当季就给收益。
6. **远地**：写 `region`；驿程超阈值时状态即时改变，军报晚一季送达——依赖该事件的奏疏应读状态旗标并接受「消息晚到」。
7. 用旗标表达事件链的阶段；repeatable 事件设 `cooldown`；同一事件未决时不会重复触发。
8. 有插图就登记 `event.<id>.illustration`（或 `illustration` 字段）。

## 输出契约

一条可验证、不会重复触发、不会抢在奏疏之前的事件定义，附测试或模拟证据。

## 涉及文件

`scenarios/<id>/events.yaml`、`memorials.yaml`、`assets.yaml`、`backend/tests/`。

## 经验（《天宝》试玩）

- 「奏疏晚于事件」（#14）、「已庇护东宫，『东宫被构』仍触发」（#10）、「平叛后粮道断绝仍持续扣兵」（#23）都是条件少写了一个守卫。
- 兵变之类的惩罚事件给玩家留一整季的反应时间（`flag_age ≥ 2`），并在前一季给出预警。
- `content_lint.py` 的 W12 会列出「事件与奏疏写同一旗标且可能先发生」的可疑对。

## 验证

`sim validate`；`python ../scripts/simulate.py <id> --seed N --trace` 看事件在哪一季触发；测试条件成立 / 失效、概率两侧、重复推进不重复触发、抉择逾期走默认项。
