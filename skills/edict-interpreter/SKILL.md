---
name: edict-interpreter
description: 维护诏书流水线：奏疏批答、政令卡、客户端 ops 与自然语言诏令到玩家层 StateOp 的解释、逐条回执、延期（稽迟 / 驿程 / 将领自主）与确认。
---

# 诏令解释

## 何时使用

调整诏令提示词、工具 schema、草案 / 确认流程、回执措辞、延期规则或政令卡展开时使用。

## 输入

本季可见奏疏与采纳的选项、政令卡与参数、客户端 ops、各类别的诏令文本、StateDigest、玩家层白名单。

## 流水线

`draft_edict {sections, adopted, ops, cards}` → 每条条款带 `source`（memorial / card / client / text）→ 规则层逐条预演 → 回执 → `confirm_edict` 才写入日志。

## 步骤

1. **来源分层**：奏疏选项是剧本层（可改任何数值）；政令卡、客户端 ops、文本解释是玩家层（白名单，越权 `OP_NOT_PERMITTED`）。确认时按同样的层级重新校验。
2. **文本解释**只在接了模型时进行：解释器只拿到合法工具 schema 与 StateDigest，信息不足时要求澄清；不支持工具调用时走 JSON 回退（同样过规则层）。离线（mock）时文本一律 `narrative_only`（`OFFLINE_TEXT`：「未接模型：御笔只记入史册」）。
3. **回执措辞**：草案写「可行（确认后本季即行）」「延至本季末结算时执行（进入第 n 回合时生效）」，确认后写「已行」「已延至……」；被拒带错误码与中文原因。摘要按条款先后累积预演（先分兵、后调余部时写分兵之后的兵额），不出现 None、原始 id 或隐藏数值。
4. **延期**分三类，回执写 `delay_kind` 与原因：有司稽迟（`formulas.execution`，按条款组掷骰）、驿程（作用于远地的条款组整组令达于下季）、将领自主（`settings.autonomy`，确认时以 (种子, 回合, 命令) 派生的随机判定，改拟无法刷；预览只给风险等级）。
5. 政令卡：参数按局势过滤并校验（`INVALID_CARD_PARAM` 列出可选值），冷却与 `requires`，有冷却的卡一道诏书只能用一次。
6. 预计抵达晚于终局时限的调动，摘要注明「本局内到不了」。
7. 单次请求 / 总时长超时、最多 12 步、可中止（`/abort`）。
8. 结算前若有拟而未确认的草案，`end_turn` 返回 `DRAFT_UNCONFIRMED`（列出编号与摘要），`discard_drafts: true` 才放弃。

## 输出契约

预览回执忠实表达将执行什么、为何被拒、为何延期、何时执行；确认前状态不变；模型永远不能越过规则层。

## 涉及文件

`backend/simcore/agent/roles.py`、`agent/tools.py`、`domain/permissions.py`、`domain/cards.py`、`domain/courier.py`、`domain/military.py`、`simcore/service.py`、`prompts/edict_interpreter.md`。

## 验证

Mock 下覆盖：采纳奏疏、政令卡、客户端 ops 越权、文本离线仅记录、JSON 回退、三类延期、未确认不变更、草案跨进程确认、`DRAFT_UNCONFIRMED`、abort。
