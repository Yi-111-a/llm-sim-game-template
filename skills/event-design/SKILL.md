---
name: event-design
description: 设计可控概率、可验证前置条件和可重放结果的场景事件。
---

# 事件设计

## 何时使用

新增历史事件、随机遭遇、条件触发或分支事件链时使用。

## 输入

事件发生时点、条件 DSL、重力、效果操作和失败后的提示。

## 步骤

1. 为事件分配稳定 id、标题与 `scheduled`、`conditional` 或 `random` 类型。
2. 在 `when` 和 `conditions` 中写出可由引擎验证的前置条件；不要用自由文本触发状态改变。
3. 将影响写成合法 `StateOp`，为缺少资源、目标失效等情况设计明确结果。
4. 为随机事件设置 `gravity`，用固定种子检查命中和未命中路径。
5. 需要事件链时用旗标表达阶段和分支，避免重复触发。
6. 如有插图，登记 `event.<id>.illustration` 并在事件引用同一 manifest ID。

## 输出契约

一条可验证、不会重复触发且带有测试的事件定义。

## 涉及文件

`scenarios/<id>/events.yaml`、`assets.yaml`、`backend/tests/`。

## 验证

测试条件成立与失效、概率两侧、重复推进不重复触发，并运行 `sim validate`。
