---
name: scenario-design
description: 设计可验证、可版本化的场景包：scenario.yaml 的资源、公式、结局与时限、难度、危机计、朝会日程、驿程、将领自主，以及奏疏这一剧情主线。
---

# 场景设计

## 何时使用

创建、扩充或审查 `scenario.yaml` 与 `memorials.yaml`；定义结局、难度、危机计或第 9 轮的朝廷 / 驿传 / 军令设置时使用。

## 输入

玩家职责、局长与回合单位、核心资源与取舍、结局与失败、引擎版本、需要表达的主题与时代感。

## 步骤

1. **元信息**：稳定 `id`、`manifest_version: 1`、语义化 `version`、`requires_engine: ">=0.3.0"`；历法 `calendar.eras`（可改元）、`turn_format`、`number_system`。
2. **资源**：少量核心资源表达取舍；看不见的威胁设为 `hidden: true` + `estimate_width`（玩家只见区间，情报旗标 `intel:<id>` 减半一次）。地区指标声明 `range`。
3. **公式**：`income`（带 `label`、`controlled_only`）、`upkeep`（按兵额养兵）、`drift`（每季漂移）、`execution`（延期概率）。开局固定开支写 `ledger`。
4. **危机计**：选一个资源做 `crisis`，`terms` 与 drift 分项一一对应；压力项只计超出部分并设上限。
5. **终局**：`endings`（fail 优先级最小）、`game_length`（时限、加时旗标、fallback）。结局正文只写条件保证的事，末尾附【达成条件】。`objectives` 写清判定口径。
6. **难度**：2–3 档 `difficulties`，用 drift / gravity / start / delay 系数，`default_difficulty` 为「标准」档。
7. **朝廷真实感**（settings）：`capital_region`、`agenda`（每季召对 / 廷议上限）、`courier`（驿程阈值，配合地区 `courier_days`）、`autonomy`（将领拒命 / 擅动）、`player_ops`（玩家白名单）、`bloc_names`、`flag_labels` / `counter_labels`、`hidden_traits`。
8. **奏疏**（剧情主线，详见 event-design 的配套规则）：
   - 每季 2–3 条以上；T0–T2 兼作教学。
   - 谏言人写 `option`（主张）与 `requires`（在场）；奏疏写 `proposal`（所议之案）。
   - **每个状态断言都有守卫**（office_holder / character_region / army_commander）。
   - 同题按局势写变体，`variant_of` + 条件互斥。
   - 远地奏疏的季数写在 `turn` 上。
   - 选项名实相符；`hint` 写全代价（养兵、行程见回执、情报只减半一次）。
   - 末季给「维持现状」。
9. 跑 `sim validate`、`content_lint`、`simulate`。

## 输出契约

完整的 `scenario.yaml` 与一批奏疏；版本、历法、结局与平衡参数清楚，三件套检查通过。

## 涉及文件

`scenarios/<id>/scenario.yaml`、`memorials.yaml`、同目录实体 YAML、`docs/SCENARIO_GUIDE.md`（字段参考）。

## 经验

- 「最差选择也能拿好结局」「明君路线全程无张力」都是平衡事故，靠 `simulate.py --policy last` 与自定义「明君」策略各跑一遍发现。
- 玩家把隐藏数值当成「可反推的谜题」：危机计若公开而分项含隐藏量，玩家能用差值反推——危机计本身也可以设为隐藏。
- 时限提示由引擎自动给出（「下次结算即终局」），不要再写同义事件；也不要在结局正文提未实现的「延长模式」。

## 验证

`cd backend && sim validate ../scenarios/<id>`；为关键历法断言首回合与跨年标签；`python ../scripts/simulate.py <id>` 各难度的分布符合设计。
