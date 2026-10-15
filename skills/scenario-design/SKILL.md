---
name: scenario-design
description: 为本地模拟游戏设计可验证、可版本化的场景包与历法。
---

# 场景设计

## 何时使用

创建、扩充或审查 `scenario.yaml`，或需要定义场景版本、日历和模组兼容范围时使用。

## 输入

玩家职责、起始日期、回合单位、资源与官职、引擎版本以及需要表达的主题。

## 步骤

1. 为场景分配稳定 `id`，设置 `manifest_version: 1`、语义化 `version` 和 `requires_engine`。
2. 为每种资源定义 id、显示名、初始值、上下限和单位；用少量核心资源表达可行动的取舍。
3. 定义区域统计字段、官职及要求、诏令类别与可安全求值的公式。不要把规则藏在提示词里。
4. 定义 `calendar.eras`、`seasons`、`turn_format`、`number_system` 和 `pre_game_turns`；确保 era `from_turn` 按回合递增。
5. 校验日期、回合单位与所有引用，再补充人物、地区和事件文件。

## 输出契约

完整的 `scenario.yaml`；版本、资源、历法字段清晰且通过场景验证。

## 涉及文件

`scenarios/<id>/scenario.yaml`、同目录实体与事件 YAML。

## 验证

执行 `cd backend && sim validate ../scenarios/<id>`；为关键历法断言第一回合格式和跨年格式。
