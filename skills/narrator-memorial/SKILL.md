---
name: narrator-memorial
description: 从已确定的回合报告撰写克制、可追溯且不虚构事实的纪事。
---

# 纪事撰写

## 何时使用

新增或调整回合纪事、历史总结或叙事模型的事实边界时使用。

## 输入

确定性引擎产出的 `TurnReport`，包含事实、账目、事件与警告。

## 步骤

1. 只将已提交的 `TurnReport` 作为可陈述事实。
2. 提示词说明不得补造人物行动、数字、对话、动机或因果。
3. 允许场景在 `prompts/narrator.md` 覆盖文风，但不改变事实来源。
4. 将纪事作为叙述输出保存，不让生成文本反向修改 `WorldState`。
5. 对空回合、事实冲突和 Mock 模式分别验证。

## 输出契约

简短、可读、仅复述事实的纪事；不产生可执行操作。

## 涉及文件

`backend/simcore/agent/roles.py`、`simcore/prompts/narrator.md`、`scenarios/<id>/prompts/`。

## 验证

提供只含有限事实的报告，检查生成文本不增加未提供的人物、数字和事件。
